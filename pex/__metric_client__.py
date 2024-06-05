#!/usr/bin/env python3

# Copy of this file is being stored in runtime at runtime/external/devtools/metric_proxy/scripts
# Any change here should will also need to be changed there.

import argparse
import atexit
import contextlib
import copy
import getpass
import json
import os
import sys
import threading
import time
from multiprocessing.pool import ThreadPool

# The requests module is not part of the standard library. Hence, depending on it
# would cause this script to fail until the module was properly provided. Since -
# the requests performed are trivial, it is better to rely on urllib, which is  -
# in the standard library, for increased compatibility
try:
    # Python 3
    from urllib.request import Request, urlopen
except ImportError:
    # Python 2
    from urllib2 import Request, urlopen

_PAYLOAD_VERSION = 5

import logging
import logging.config

mclogger = logging.getLogger(__name__)
"""
Python 3 features note
 - All the type hints were omitted to keep compatibility with Python 2
 - f'Strings were not used because they were added on Python 3.6
"""


class MetricProxyTimer(object):
    """
    Monitors the execution of a code block and emits a metric (and an event) at the end with the
    duration (milliseconds), status (success, failure) depending if an exception was thrown, and
    any other extra labels passed by the client
    """

    def __init__(self,
                 metric_name,
                 service,
                 metric_type="gauge",
                 buckets=None,
                 description="",
                 labels={},
                 send_event=False,
                 send_async=True,
                 client=None):
        self.metric_type = metric_type
        self.metric_name = metric_name
        self.buckets = buckets
        self.service = service
        self.labels = labels
        self.description = description
        self.send_event = send_event
        self.client = client or MonitoringClient.get_default()
        self.send_async = send_async

    def __enter__(self):
        self.start = round(time.time() * 1000)

    def __exit__(self, exc_type, exc_val, exc_traceback):
        end = round(time.time() * 1000)
        self.labels["status"] = "failure" if exc_type else "success"
        metric = Metric(
            metric_type=self.metric_type,
            service=self.service,
            name=self.metric_name,
            value=end - self.start,
            buckets=self.buckets,
            labels=self.labels,
            description=self.description,
            unit="milliseconds")
        if self.send_async:
            self.client.record_usage(metric)
        else:
            self.client.record_usage_sync(metric)

        if self.send_event:
            # We need to deep copy the self.labels in order to prevent race conditions when
            # the duration label, that should only be send to the event, end up being reported
            # as part of the metric
            event_labels = copy.deepcopy(self.labels)
            event_labels["metric_name"] = self.metric_name
            event_labels["duration"] = str(end - self.start)
            event = Event(service=self.service, tags=event_labels)
            if self.send_async:
                self.client.record_event(event)
            else:
                self.client.record_event_sync(event)


class MonitoringClient(object):
    """
    A client exposed to Python programs that wants to ship custom metrics
    """

    _DEFAULT_CONTEXT = {
        "timeout": 3,
        "urls": {
            "local": "http://localhost:8080",
            "dev": "https://metric-proxy.dev.databricks.com",
            "staging": "https://metric-proxy.staging.cloud.databricks.com"
        },
        "verbose": False,
        "threads": 10
    }
    _INSTANCE = None

    def __init__(self, context=None, environment="staging"):
        self._context = {
            "timeout": self._context_or_default("timeout", context),
            "urls": self._context_or_default("urls", context),
            "threads": self._context_or_default("threads", context),
            "verbose": self._context_or_default("verbose", context),
        }
        url = self._context["urls"][environment or "staging"]
        self._http_client = _HttpClient(url)
        _Utils.setup_logger(self._context["verbose"])
        self._executor = ThreadPool(self._context["threads"])

        major, minor = _Utils.get_version()
        if major >= 3 and minor >= 12:
            # HANDLING GRACEFUL SHUTDOWN ON cpython >= 3.12 (PLAT-102226)
            # -----------------------------------------------------------
            # - We can no longer create daemon threads on atexit (https://github.com/python/cpython/pull/104826)
            # - We relied on this to setup an upper limit to wait for the thread pool to shutdown
            # - Workaround:
            #     1. Create the shutdown thread in the init and set daemon to true
            #        - Setting daemon to true instructs the interpreter to not wait for the thread when exiting
            #     2. Put it to sleep until the shutdown event is sent
            #     3. Upon wake up, joining the workers from the pool
            #     4. Join the shutdown thread with the upper limit from the main thread
            self._shutdown_event = threading.Event()
            self._shutdown_thread = threading.Thread(target=self._shutdown_pool)
            self._shutdown_thread.daemon = True
            self._shutdown_thread.start()
            atexit.register(self.shutdown_3_12)
        else:
            atexit.register(self.shutdown)

    @staticmethod
    def get_default(context=None, environment="staging"):
        """
        :return: a singleton object with always a default context
        """
        if not MonitoringClient._INSTANCE:
            MonitoringClient._INSTANCE = MonitoringClient(context=context, environment=environment)
        return MonitoringClient._INSTANCE

    @staticmethod
    def _context_or_default(key, new_context):
        return new_context[
            key] if new_context and key in new_context else MonitoringClient._DEFAULT_CONTEXT[key]

    def record_usage(self, metric):
        return self._executor.apply_async(self._http_client.post_metric, [metric])

    def record_usage_sync(self, metric):
        return self._http_client.post_metric(metric)

    def record_event(self, event):
        return self._executor.apply_async(self._http_client.post_event, [event])

    def record_event_sync(self, event):
        return self._http_client.post_event(event)

    def shutdown(self):
        """
        WARNING: Do not rely on __del__ to perform the shutdown as there is no guarantee that it
        will be invoked by the garbage collector
        """
        try:
            try:
                # Tells the executor to stop accepting new jobs
                self._executor.close()
                # Run the join method in a separate thread to allow stopping it on the given timeout
                # time we do that because Python 2 doesn't seem to offer a better API for that
                thread = threading.Thread(target=self._executor.join)
                thread.daemon = True
                thread.start()
                thread.join(self._context["timeout"])
            except RuntimeError as e:
                if str(e) == "can't create new thread at interpreter shutdown":
                    mclogger.warning(
                        "Failed to create a new thread to join Metric Proxy thread pool (PLAT-102226)"
                    )
                else:
                    raise
        except Exception as e:
            mclogger.error("An exception has happened while joining Metric Proxy thread pool: %s",
                           str(e))

    def shutdown_3_12(self):
        if self._shutdown_event and self._shutdown_thread:
            # Wake up the thread that is waiting for the shutdown event
            self._shutdown_event.set()
            # Wait for that thread to end
            self._shutdown_thread.join(self._context["timeout"])
        else:
            msg = "shutdown_3_12 called but shutdown_[event, thread] is None!. Please report to #devex"
            mclogger.error(msg)

    def _shutdown_pool(self):
        try:
            self._shutdown_event.wait()
            self._executor.close()
            self._executor.join()
        except:
            error = "(no-op) metric-proxy graceful shutdown failed. Please report to #devex"
            mclogger.warn(error)


class Event:
    """
    A data structure that holds an event, a high-cardinality data to be stored on Delta tables
    See: go/metriccardinality
    """

    def __init__(self, service, tags):
        environment = _Utils.get_environment()
        self._event = {
            "service": service,
            "version": [_PAYLOAD_VERSION],
            "tags": {
                "user": getpass.getuser(),
                "timestamp": str(_Utils.now()),
                "source": environment,
                "environment": environment,
                "service": service,
            }
        }
        self._event["tags"].update(_Utils.get_environment_labels(environment))
        self._event["tags"].update(_Utils.get_event_default_labels(environment))
        # All tag values should be string to respect the contract with the backend
        self._event["tags"].update(_Utils.cast_values_to_str(tags))

    @staticmethod
    def is_valid():
        return True

    def as_dict(self):
        return self._event

    def as_json(self):
        return json.dumps(self._event, indent=4)

    @staticmethod
    def from_args(args):
        tags = dict(tag.strip().split(":", 1) for tag in args.tags)
        if args.tag_files:
            for kf in args.tag_files:
                (k, fname) = kf.split(":", 1)
                with open(fname, encoding='utf8') as f:
                    tags[k] = f.read()
        return Event(
            service=args.service,
            tags=tags,
        )


class Metric:
    """
    A data structure that holds a metric, a low-cardinality data in the time-series shape to be stored in M3
    See: go/metriccardinality
    """
    """
        - A counter is an ever increasing value and each metric recorded increases the counter.
        An example are the number of API calls.
   
        - A gauge is a metric is has a certain value and each metric recorded records a new value.
        An example are the number of API rate limited calls left in an hour.
   
        - A summary samples observations (usually things like request durations and response sizes).
        While it also provides a total count of observations and a sum of all observed values, it 
        calculates configurable quantiles over a sliding time window. 
   
        A histogram is a more limited version of metric where instead of recording the exact value,
        the information is recorded in pre-defined buckets.
    """
    SUPPORTED_TYPES = ["counter", "gauge", "summary", "histogram"]
    _DEFAULT_DESCRIPTION = "A %s generated from DevTools Metric Proxy CLI"

    def __init__(self,
                 metric_type,
                 service,
                 name,
                 value,
                 unit,
                 retention="retain",
                 buckets="",
                 quantiles="",
                 labels="",
                 description=None):
        self._metric_type = metric_type
        self._service = service
        self._name = name
        self._value = value
        self._unit = unit
        self._retention = retention
        self._buckets = buckets
        self._quantiles = quantiles
        self._description = description if description else Metric._DEFAULT_DESCRIPTION % name
        self._labels = {}
        self._build_labels(labels)
        self._timestamp = _Utils.now()
        self._user = getpass.getuser()
        self._version = [_PAYLOAD_VERSION]

    def is_valid(self):
        if not self._service:
            return False
        if self._metric_type not in Metric.SUPPORTED_TYPES:
            return False
        if self._buckets:
            aux = map(lambda x: _Utils.is_number(x), self._buckets.split(","))
            return False if False in aux else True
        if not _Utils.is_number(self._value):
            return False
        return True

    def as_dict(self):
        return {
            "metric_type": self._metric_type,
            "name": self._name,
            "value": self._value,
            "unit": self._unit,
            "retention": self._retention,
            "description": self._description,
            "buckets": self._buckets,
            "quantiles": self._quantiles,
            "labels": self._labels,
            "timestamp": self._timestamp,
            "user": self._user,
            "version": self._version,
        }

    def as_json(self):
        return json.dumps(self.as_dict(), indent=4)

    @staticmethod
    def from_args(args):
        return Metric(
            metric_type=args.which,
            service=args.service,
            name=args.name,
            value=args.value,
            unit=args.unit,
            retention=args.retention,
            buckets=args.buckets if "buckets" in args else "",
            quantiles=args.quantiles if "quantiles" in args else "",
            labels=args.labels,
            description=args.description,
        )

    def _build_labels(self, raw_labels):
        self._labels = raw_labels if raw_labels else {}
        if type(raw_labels) == str:
            # Filter for labels in the shape <key>:<value>
            self._labels = filter(lambda x: len(x.split(":")) == 2, raw_labels.split(","))
            # Maps the list into a dict {key: value}
            self._labels = dict(
                (label.split(":")[0], label.split(":")[1]) for label in self._labels)
        environment = _Utils.get_environment()
        self._labels["environment"] = environment
        self._labels.update(_Utils.get_environment_labels(environment))
        # Keeping last since the default labels should not be overwritten
        self._labels["service"] = self._service
        self._labels.update(_Utils.get_metric_default_labels())


class _HttpClient:
    """
    Encapsulates the logic to post metrics or events to DevTools Metric Proxy
    """

    # How long to wait for the server response
    TIMEOUT_IN_SECONDS = 3

    def __init__(self, base_url):
        self._base_url = base_url

    def post_metric(self, metric):
        url = "%s/%s" % (self._base_url, "metric")
        return _HttpClient._post(url, metric)

    def post_event(self, event):
        url = "%s/%s" % (self._base_url, "event")
        return _HttpClient._post(url, event)

    @staticmethod
    def _post(url, element):
        try:
            if not element.is_valid():
                mclogger.warning("%s is invalid" % element.as_json())
                return False
            mclogger.debug("Posting: %s" % element.as_json())
            return _HttpClient._do_post(url, element.as_dict())
        except Exception as e:
            # We prevent exceptions from leaking because the Metric Client should not interfere with the main program
            # Failed to post exceptions should generate an error log but not kill the main program
            mclogger.error("Failed to post data to Metric-Client: %s" % str(e))
            return False

    @staticmethod
    def _do_post(url, payload):
        try:
            with contextlib.closing(
                    urlopen(
                        _HttpClient._build_request(url),
                        data=json.dumps(payload, indent=4).encode(),
                        timeout=_HttpClient.TIMEOUT_IN_SECONDS)) as conn:
                response = conn.read().decode()
        except Exception as exc:
            mclogger.error("Post error: %s" % str(exc))
            return False
        else:
            mclogger.info(response)
        return True

    @staticmethod
    def _build_request(url):
        version = sys.version_info
        request = Request(
            url, method="POST") if version[0] == 3 and version[1] > 2 else Request(url)
        request.add_header('Content-Type', 'application/json')
        return request


class _Utils:
    """
    Set of utilities to be used in the program
    """

    @staticmethod
    def is_number(number):
        try:
            float(number)
            return True
        except ValueError:
            return False

    @staticmethod
    def now():
        return int(round(time.time() * 1000))

    @staticmethod
    def read_labels_from_file(src):
        try:
            env = {}
            if not os.path.exists(src):
                return env
            with open(src) as fp:
                for line in fp:
                    label, value = line.split(" ", 1)
                    env[label.lower()] = value.rstrip("\n")
            return env
        except Exception as ex:
            logging.error("Error reading labels: %s", ex)
            return {}

    # Assumption: default labels don't change during the execution. If they
    #             were to change it means that the caller can compute the values
    #             and could thus pass them explicitly
    _default_labels = None
    _default_labels_mutex = threading.Lock()

    @staticmethod
    def get_default_labels():
        if _Utils._default_labels is None:
            with _Utils._default_labels_mutex:
                if _Utils._default_labels is None:
                    src = "/etc/metric_proxy/labels/default"
                    _Utils._default_labels = _Utils.read_labels_from_file(src)
                    mclogger.debug("Loaded default labels " + str(_Utils._default_labels))
        return _Utils._default_labels

    @staticmethod
    def get_environment_labels(environment):
        env = {"build_name": _Utils.get_default_labels().get("build_name", "null")}
        if environment == "runbot" and "RUNBOT_BUILD_NAME" in os.environ:
            env["build_name"] = os.environ["RUNBOT_BUILD_NAME"]
        elif environment == "jenkins" and "JOB_NAME" in os.environ:
            env["build_name"] = os.environ["JOB_NAME"]
        return env

    @staticmethod
    def get_event_default_labels(environment):
        default = "null"
        env = {"env_url": default, "build_id": default}
        env.update(_Utils.get_default_labels())
        if environment == "runbot":
            env["env_url"] = os.environ.get("RUNBOT_HOST_URL", env.get("env_url", default))
            env["build_id"] = os.environ.get("RUNBOT_RUN_ID", env.get("build_id", default))
        elif environment == "jenkins":
            env["env_url"] = os.environ.get("JENKINS_URL", env.get("env_url", default))
            env["build_id"] = os.environ.get("BUILD_NUMBER", env.get("env_url", default))

        return env

    @staticmethod
    def get_metric_default_labels():
        src = "/etc/metric_proxy/labels/metric"
        return _Utils.read_labels_from_file(src)

    _environment = None
    _environment_lock = threading.Lock()

    @staticmethod
    def get_environment():
        def fromfile(src):
            if not os.path.exists(src):
                return None
            with open(src) as fp:
                env = fp.read().strip()
            if env in ("runbot", "jenkins", "devbox", "laptop", "phoenix"):
                mclogger.debug("Loaded env: " + env)
                return env
            mclogger.warning("Unknown env: " + env)
            return "unknown"

        try:
            if _Utils._environment is None:
                with _Utils._environment_lock:
                    if _Utils._environment is None:
                        if "RUNBOT_HOST_URL" in os.environ or "RUNBOT_RUN_ID" in os.environ:
                            _Utils._environment = "runbot"
                        elif "JENKINS_URL" in os.environ or \
                                getpass.getuser().lower() == "jenkins" or \
                                "BUILD_NUMBER" in os.environ:
                            _Utils._environment = "jenkins"
                        elif "LC_DB_DEVBOX" in os.environ:
                            _Utils._environment = "devbox"
                        else:
                            _Utils._environment = fromfile("/etc/metric_proxy/env") or "laptop"
            return _Utils._environment
        except Exception as ex:
            logging.error("Error discovering environment: %s", ex)
            return "failed"

    @staticmethod
    def setup_logger(verbose=False):
        # See: https://docs.python.org/2.7/library/logging.config.html#logging-config-dictschema
        config = dict(
            version=1,
            disable_existing_loggers=False,
            # key == formatter id value == config for the corresponding Formatter instance
            formatters={
                "metric_client_formatter": {
                    "format": "[%(levelname)s] - %(message)s"
                },
            },
            # key == handler id value == config for the corresponding Handler instance
            handlers={
                "metric_client_handler": {
                    "level": "INFO" if verbose else "ERROR",
                    "formatter": "metric_client_formatter",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                },
            },
            # key == logger value == config for the corresponding Logger instance
            loggers={
                __name__: {
                    "handlers": ["metric_client_handler"],
                    "level": os.environ.get("METRIC_PROXY_LOGGING_LEVEL",
                                            "INFO" if verbose else "ERROR"),
                    "propagate": False
                }
            },
        )
        logging.config.dictConfig(config)

    @staticmethod
    def cast_values_to_str(obj):
        try:
            return {str(k): str(v) for k, v in obj.items()}
        except Exception as e:
            mclogger.error("Unable to cast obj to str", (obj, e))
            return {}

    @staticmethod
    def get_version():
        major, minor, _ = sys.version_info[:3]
        return major, minor


class _Parser:
    """
    Builds a parser that accepts metrics / events to be synced with remote
    """

    def __init__(self, version):
        self.parser = argparse.ArgumentParser(description="A proxy for metrics")
        self.parser.set_defaults(run=lambda *_: self.parser.print_help())

        self.subparsers = self.parser.add_subparsers(help="commands")
        self.parser.add_argument("-v", "--version", action="version", version=version)
        self.parser.add_argument(
            "-e",
            "--environment",
            default="staging",
            help="To which instance of Metric Proxy the data will be sent to")
        self.parser.add_argument(
            "-ve", "--verbose", action="store_true", help="Enable verbose mode")
        self._add_prom_metric_default_sub_parser("counter")
        self._add_prom_metric_default_sub_parser("gauge")
        histogram = self._add_prom_metric_default_sub_parser("histogram")
        histogram.add_argument(
            "--buckets",
            "-b",
            required=True,
            default=None,
            help="the buckets in which the histogram sample should be placed",
        )
        summary = self._add_prom_metric_default_sub_parser("summary")
        summary.add_argument(
            "--quantiles",
            "-q",
            required=True,
            default=None,
            help="a comma-separated list of quantile:tolerate_error. Example: 0.5:0.05,0.75:0.001",
        )
        self._add_event_sub_parser()

    def execute(self):
        args = self.parser.parse_known_args()[0]
        context = {}
        if args.verbose:
            context["verbose"] = True
        args.run(context, args)

    def _add_prom_metric_default_sub_parser(self, name):
        def initialize(context, args):
            MonitoringClient(context, args.environment).record_usage(Metric.from_args(args))

        prom_metric = self.subparsers.add_parser(name)
        prom_metric.set_defaults(run=initialize)
        prom_metric.set_defaults(which=name)
        prom_metric.add_argument(
            "--service",
            "-s",
            help="The origin of the metric",
            required=True,
        )
        prom_metric.add_argument(
            "--name",
            "-n",
            help="The name of the metric",
            required=True,
        )
        prom_metric.add_argument(
            "--value",
            "-v",
            help="The metric value",
            required=True,
        )
        prom_metric.add_argument(
            "--unit",
            "-u",
            help="The unit of the metric value",
            required=True,
            choices=["info", "ratio", "count", "seconds", "milliseconds", "bytes"])
        prom_metric.add_argument(
            "--retention",
            "-r",
            default="retain",
            help="The retention type for the metric",
            choices=["delete_after_push", "retain"])
        prom_metric.add_argument(
            "--description",
            "-d",
            default="",
            help="The description of the metric",
        )
        prom_metric.add_argument(
            "--labels",
            "-l",
            default="",
            help="the metric labels in the format: k1:v1,k2:v2",
        )
        return prom_metric

    def _add_event_sub_parser(self):
        def initialize(context, args):
            MonitoringClient(context, args.environment).record_event(Event.from_args(args))

        event = self.subparsers.add_parser("event")
        event.set_defaults(run=initialize)
        event.add_argument(
            "--service",
            "-s",
            help="the name of the service this event is generated from",
            required=True,
        )
        event.add_argument(
            "--tags",
            "-t",
            nargs="+",
            help="The event tags in the form k1:v1",
            required=True,
        )
        event.add_argument(
            "--tag-files",
            nargs="+",
            help="The event tags in the form k1:file1",
            required=False,
        )


if __name__ == "__main__":
    try:
        _Parser("0.0.1").execute()
    except Exception as e:
        # We prevent exceptions from leaking because the Metric Client should not interfere with the main program
        # Failed to post exceptions should generate an error log but not kill the main program
        mclogger.error("Failed to post data to Metric-Client")
        import traceback

        traceback.print_exc()
