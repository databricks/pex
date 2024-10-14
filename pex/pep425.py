# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""PEP425 handling for pex

PEP425 (http://legacy.python.org/dev/peps/pep-0425/) describes a tagging system used to determine
whether or not a distribution's platform is compatible with the current platform.  It is the
tagging system used to describe platform compatibility for wheel files.
"""

from pkg_resources import get_supported_platform

from .platforms import Platform


class PEP425Extras(object):
  """Extensions to platform handling beyond PEP425."""

  @classmethod
  def is_macosx_platform(cls, platform):
    return platform.startswith('macosx')

  @classmethod
  def parse_macosx_tag(cls, platform_tag):
    invalid_tag = ValueError('invalid macosx tag: %s' % platform_tag)
    if not cls.is_macosx_platform(platform_tag):
      raise invalid_tag
    segments = platform_tag.split('_', 3)
    if len(segments) < 3:
      raise invalid_tag
    if segments[0] != 'macosx':
      raise invalid_tag
    try:
      major = int(segments[1])
      try:
        minor = int(segments[2])
        platform = segments[3]
      except ValueError:
        minor = 0
        platform = "_".join(segments[2:])
      except IndexError:
        raise invalid_tag
    except ValueError:
      raise invalid_tag
    return major, minor, platform

  @classmethod
  # From https://github.com/pantsbuild/pex/blob/97a2497e0938ece709310a03e7e41b5c26992952/pex/vendor/_vendored/packaging_21_3/packaging/tags.py#L313:5
  def _mac_binary_formats(cls, version_tuple, cpu_arch):
    formats = [cpu_arch]
    if cpu_arch == "x86_64":
        if version_tuple < (10, 4):
            return []
        formats.extend(["intel", "fat64", "fat32"])

    elif cpu_arch == "i386":
        if version_tuple < (10, 4):
            return []
        formats.extend(["intel", "fat32", "fat"])

    elif cpu_arch == "ppc64":
        # TODO: Need to care about 32-bit PPC for ppc64 through 10.2?
        if version_tuple > (10, 5) or version_tuple < (10, 4):
            return []
        formats.append("fat64")

    elif cpu_arch == "ppc":
        if version_tuple > (10, 6):
            return []
        formats.extend(["fat32", "fat"])

    if cpu_arch in {"arm64", "x86_64"}:
        formats.append("universal2")

    if cpu_arch in {"x86_64", "i386", "ppc64", "ppc", "intel"}:
        formats.append("universal")

    return formats


  @classmethod
  def iter_compatible_osx_platforms(cls, supported_platform):
    major, minor, arch = cls.parse_macosx_tag(supported_platform)
    version = (major, minor)

    # From https://github.com/pantsbuild/pex/blob/97a2497e0938ece709310a03e7e41b5c26992952/pex/vendor/_vendored/packaging_21_3/packaging/tags.py#L366-L414
    if (10, 0) <= version and version < (11, 0):
        # Prior to Mac OS 11, each yearly release of Mac OS bumped the
        # "minor" version number.  The major version was always 10.
        for minor_version in range(version[1], -1, -1):
            compat_version = 10, minor_version
            binary_formats = cls._mac_binary_formats(compat_version, arch)
            for binary_format in binary_formats:
                yield "macosx_{major}_{minor}_{binary_format}".format(
                    major=10, minor=minor_version, binary_format=binary_format
                )

    if version >= (11, 0):
        # Starting with Mac OS 11, each yearly release bumps the major version
        # number.   The minor versions are now the midyear updates.
        for major_version in range(version[0], 10, -1):
            compat_version = major_version, 0
            binary_formats = cls._mac_binary_formats(compat_version, arch)
            for binary_format in binary_formats:
                yield "macosx_{major}_{minor}_{binary_format}".format(
                    major=major_version, minor=0, binary_format=binary_format
                )

    if version >= (11, 0):
        # Mac OS 11 on x86_64 is compatible with binaries from previous releases.
        # Arm64 support was introduced in 11.0, so no Arm binaries from previous
        # releases exist.
        #
        # However, the "universal2" binary format can have a
        # macOS version earlier than 11.0 when the x86_64 part of the binary supports
        # that version of macOS.
        if arch == "x86_64":
            for minor_version in range(16, 3, -1):
                compat_version = 10, minor_version
                binary_formats = cls._mac_binary_formats(compat_version, arch)
                for binary_format in binary_formats:
                    yield "macosx_{major}_{minor}_{binary_format}".format(
                        major=compat_version[0],
                        minor=compat_version[1],
                        binary_format=binary_format,
                    )
        else:
            for minor_version in range(16, 3, -1):
                compat_version = 10, minor_version
                binary_format = "universal2"
                yield "macosx_{major}_{minor}_{binary_format}".format(
                    major=compat_version[0],
                    minor=compat_version[1],
                    binary_format=binary_format,
                )

  @classmethod
  def platform_iterator(cls, platform):
    """Iterate over all compatible platform tags of a supplied platform tag.

       :param platform: the platform tag to iterate over
    """
    if cls.is_macosx_platform(platform):
      for plat in cls.iter_compatible_osx_platforms(platform):
        yield plat
    else:
      yield platform


class PEP425(object):  # noqa
  INTERPRETER_TAGS = {
    'CPython': 'cp',
    'Jython': 'jy',
    'PyPy': 'pp',
    'IronPython': 'ip',
  }

  @classmethod
  def get_implementation_tag(cls, interpreter_subversion):
    return cls.INTERPRETER_TAGS.get(interpreter_subversion)

  @classmethod
  def get_version_tag(cls, interpreter_version):
    return ''.join(map(str, interpreter_version[:2]))

  @classmethod
  def translate_platform_to_tag(cls, platform):
    return platform.replace('.', '_').replace('-', '_')

  @classmethod
  def get_platform_tag(cls):
    return cls.translate_platform_to_tag(get_supported_platform())

  # TODO(wickman) This implementation is technically incorrect but we need to be able to
  # predict the supported tags of an interpreter that may not be on this machine or
  # of a different platform.  Alternatively we could store the manifest of supported tags
  # of a targeted platform in a file to be more correct.
  @classmethod
  def _iter_supported_tags(cls, impl, version, platform):
    """Given a set of tags, iterate over supported tags.

    :param impl: Python implementation tag e.g. cp, jy, pp.
    :param version: E.g. '26', '33'
    :param platform: Platform as from :function:`pkg_resources.get_supported_platform`,
    for example 'linux-x86_64' or 'macosx-10.4-x86_64'.
    :returns: Iterator over (pyver, abi, platform) tuples.
    """
    # Predict soabi for reasonable interpreters.  This is technically wrong but essentially right.
    abis = []
    if impl == 'cp' and (version.startswith('2') or version.startswith('3')):
      abis.extend([
        'cp%s' % version,
        'cp%sdmu' % version, 'cp%sdm' % version, 'cp%sdu' % version, 'cp%sd' % version,
        'cp%smu' % version, 'cp%sm' % version,
        'cp%su' % version
      ])

      if version.startswith('3'):
        abis.extend([
          'abi3'
        ])

    major_version = int(version[0])
    minor_versions = []
    for minor in range(int(version[1:]), -1, -1):
      minor_versions.append('%d%d' % (major_version, minor))
    platforms = list(PEP425Extras.platform_iterator(cls.translate_platform_to_tag(platform)))

    # interpreter specific
    for p in platforms:
      for abi in abis:
        yield ('%s%s' % (impl, version), abi, p)

    # everything else
    for p in platforms + ['any']:
      for i in ('py', impl):
        yield ('%s%d' % (i, major_version), 'none', p)
        for minor_version in minor_versions:
          yield ('%s%s' % (i, minor_version), 'none', p)

  @classmethod
  def iter_supported_tags(cls, identity, platform=get_supported_platform()):
    """Iterate over the supported tag tuples of this interpreter.

    :param identity: python interpreter identity over which tags should iterate.
    :type identity: :class:`PythonIdentity`
    :param platform: python platform over which tags should iterate, by default the current
                     platform.
    :returns: Iterator over valid PEP425 tag tuples.
    """
    impl_tag = cls.get_implementation_tag(identity.interpreter)
    vers_tag = cls.get_version_tag(identity.version)
    tag_iterator = cls._iter_supported_tags(impl_tag, vers_tag, platform)
    for tag in tag_iterator:
      yield tag
