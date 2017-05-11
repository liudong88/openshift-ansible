#!/usr/bin/python
'''
Ansible module for yum-based systems determining if multiple releases
of an OpenShift package are available, and if the release requested
(if any) is available down to the given precision.

For Enterprise, multiple releases available suggest that multiple repos
are enabled for the different releases, which may cause installation
problems. With Origin, however, this is a normal state of affairs as
all the releases are provided in a single repo with the expectation that
only the latest can be installed.

Code in the openshift_version role contains a lot of logic to pin down
the exact package and image version to use and so does some validation
of release availability already. Without duplicating all that, we would
like the user to have a helpful error message if we detect things will
not work out right. Note that if openshift_release is not specified in
the inventory, the version comparison checks just pass.

TODO: fail gracefully on non-yum systems (dnf in Fedora)
'''

from ansible.module_utils.basic import AnsibleModule

IMPORT_EXCEPTION = None
try:
    import yum  # pylint: disable=import-error
except ImportError as err:
    IMPORT_EXCEPTION = err  # in tox test env, yum import fails


class AosVersionException(Exception):
    '''Base exception class for package version problems'''
    def __init__(self, message, problem_pkgs=None):
        Exception.__init__(self, message)
        self.problem_pkgs = problem_pkgs


def main():
    '''Entrypoint for this Ansible module'''
    module = AnsibleModule(
        argument_spec=dict(
            requested_openshift_release=dict(type="str", default=''),
            openshift_deployment_type=dict(required=True),
            rpm_prefix=dict(required=True),  # atomic-openshift, origin, ...?
        ),
        supports_check_mode=True
    )

    if IMPORT_EXCEPTION:
        module.fail_json(msg="aos_version module could not import yum: %s" % IMPORT_EXCEPTION)

    # determine the packages we will look for
    rpm_prefix = module.params['rpm_prefix']
    if not rpm_prefix:
        module.fail_json(msg="rpm_prefix must not be empty")
    expected_pkgs = set([
        rpm_prefix,
        rpm_prefix + '-master',
        rpm_prefix + '-node',
    ])

    # determine what level of precision the user specified for the openshift version.
    # should look like a version string with possibly many segments e.g. "3.4.1":
    requested_openshift_release = module.params['requested_openshift_release']

    # get the list of packages available and complain if anything is wrong
    try:
        pkgs = _retrieve_available_packages(expected_pkgs)
        if requested_openshift_release:
            _check_precise_version_found(pkgs, expected_pkgs, requested_openshift_release)
            _check_higher_version_found(pkgs, expected_pkgs, requested_openshift_release)
        if module.params['openshift_deployment_type'] in ['openshift-enterprise']:
            _check_multi_minor_release(pkgs, expected_pkgs)
    except AosVersionException as excinfo:
        module.fail_json(msg=str(excinfo))
    module.exit_json(changed=False)


def _retrieve_available_packages(expected_pkgs):
    # search for package versions available for openshift pkgs
    yb = yum.YumBase()  # pylint: disable=invalid-name

    # The openshift excluder prevents unintended updates to openshift
    # packages by setting yum excludes on those packages. See:
    # https://wiki.centos.org/SpecialInterestGroup/PaaS/OpenShift-Origin-Control-Updates
    # Excludes are then disabled during an install or upgrade, but
    # this check will most likely be running outside either. When we
    # attempt to determine what packages are available via yum they may
    # be excluded. So, for our purposes here, disable excludes to see
    # what will really be available during an install or upgrade.
    yb.conf.disable_excludes = ['all']

    try:
        pkgs = yb.pkgSack.returnPackages(patterns=expected_pkgs)
    except yum.Errors.PackageSackError as excinfo:
        # you only hit this if *none* of the packages are available
        raise AosVersionException('\n'.join([
            'Unable to find any OpenShift packages.',
            'Check your subscription and repo settings.',
            str(excinfo),
        ]))
    return pkgs


class PreciseVersionNotFound(AosVersionException):
    '''Exception for reporting packages not available at given release'''
    def __init__(self, requested_release, not_found):
        msg = ['Not all of the required packages are available at requested version %s:' % requested_release]
        msg += ['  ' + name for name in not_found]
        msg += ['Please check your subscriptions and enabled repositories.']
        AosVersionException.__init__(self, '\n'.join(msg), not_found)


def _check_precise_version_found(pkgs, expected_pkgs, requested_openshift_release):
    # see if any packages couldn't be found at requested release version
    # we would like to verify that the latest available pkgs have however specific a version is given.
    # so e.g. if there is a package version 3.4.1.5 the check passes; if only 3.4.0, it fails.

    pkgs_precise_version_found = {}
    for pkg in pkgs:
        if pkg.name not in expected_pkgs:
            continue
        # does the version match, to the precision requested?
        # and, is it strictly greater, at the precision requested?
        match_version = '.'.join(pkg.version.split('.')[:requested_openshift_release.count('.') + 1])
        if match_version == requested_openshift_release:
            pkgs_precise_version_found[pkg.name] = True

    not_found = []
    for name in expected_pkgs:
        if name not in pkgs_precise_version_found:
            not_found.append(name)

    if not_found:
        raise PreciseVersionNotFound(requested_openshift_release, not_found)


class FoundHigherVersion(AosVersionException):
    '''Exception for reporting that a higher version than requested is available'''
    def __init__(self, requested_release, higher_found):
        msg = ['Some required package(s) are available at a version',
               'that is higher than requested %s:' % requested_release]
        msg += ['  ' + name for name in higher_found]
        msg += ['This will prevent installing the version you requested.']
        msg += ['Please check your enabled repositories or adjust openshift_release.']
        AosVersionException.__init__(self, '\n'.join(msg), higher_found)


def _check_higher_version_found(pkgs, expected_pkgs, requested_openshift_release):
    req_release_arr = [int(segment) for segment in requested_openshift_release.split(".")]
    # see if any packages are available in a version higher than requested
    higher_version_for_pkg = {}
    for pkg in pkgs:
        if pkg.name not in expected_pkgs:
            continue
        version = [int(segment) for segment in pkg.version.split(".")]
        too_high = version[:len(req_release_arr)] > req_release_arr
        higher_than_seen = version > higher_version_for_pkg.get(pkg.name, [])
        if too_high and higher_than_seen:
            higher_version_for_pkg[pkg.name] = version

    if higher_version_for_pkg:
        higher_found = []
        for name, version in higher_version_for_pkg.items():
            higher_found.append(name + '-' + '.'.join(str(segment) for segment in version))
        raise FoundHigherVersion(requested_openshift_release, higher_found)


class FoundMultiRelease(AosVersionException):
    '''Exception for reporting multiple minor releases found for same package'''
    def __init__(self, multi_found):
        msg = ['Multiple minor versions of these packages are available']
        msg += ['  ' + name for name in multi_found]
        msg += ["There should only be one OpenShift release repository enabled at a time."]
        AosVersionException.__init__(self, '\n'.join(msg), multi_found)


def _check_multi_minor_release(pkgs, expected_pkgs):
    # see if any packages are available in more than one minor version
    pkgs_by_name_version = {}
    for pkg in pkgs:
        # keep track of x.y (minor release) versions seen
        minor_release = '.'.join(pkg.version.split('.')[:2])
        if pkg.name not in pkgs_by_name_version:
            pkgs_by_name_version[pkg.name] = {}
        pkgs_by_name_version[pkg.name][minor_release] = True

    multi_found = []
    for name in expected_pkgs:
        if name in pkgs_by_name_version and len(pkgs_by_name_version[name]) > 1:
            multi_found.append(name)

    if multi_found:
        raise FoundMultiRelease(multi_found)


if __name__ == '__main__':
    main()
