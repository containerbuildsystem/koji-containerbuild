# Enable Python 3 builds for Fedora/RHEL8
%if 0%{?fedora} || 0%{?rhel} >= 8
%bcond_without python3
# If the definition isn't available for python3_pkgversion, define it
%{?!python3_pkgversion:%global python3_pkgversion 3}
%else
%bcond_with python3
%endif

%if 0%{?rhel} && 0%{?rhel} <= 6
%{!?__python2: %global __python2 /usr/bin/python2}
%{!?python2_sitelib: %global python2_sitelib %(%{__python2} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}
%endif

%global module koji_containerbuild

Name:           koji-containerbuild
Version:        1.3.0
Release:        1%{?dist}
Summary:        Koji support for building layered container images
Group:          Applications/System

License:        LGPLv2
URL:            https://github.com/containerbuildsystem/%{name}
Source0:        https://github.com/containerbuildsystem/%{name}/archive/%{version}.tar.gz
BuildArch:      noarch

%if 0%{with python3}
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
%else
BuildRequires:  python2-devel
BuildRequires:  python-setuptools
%endif

%description
Koji support for building layered container images

%if 0%{with python3}
%package hub
License:    LGPLv2
Summary:    Hub plugin that extend Koji to build layered container images
Group:      Applications/System
Requires:   koji-containerbuild
Requires:   koji-hub

%description hub
Hub plugin that extend Koji to support building layered container images


%package builder
License:    LGPLv2
Summary:    Builder plugin that extend Koji to build layered container images
Group:      Applications/System
Requires:   koji-builder >= 1.26
Requires:   koji-containerbuild
Requires:   osbs-client >= 2.0.0
Requires:   python3-dockerfile-parse
Requires:   python3-jsonschema
Requires:   python3-six


%description builder
Builder plugin that extend Koji to communicate with OpenShift build system and
build layered container images.
# end with_python3
%endif

%package -n python2-%{name}-cli
License:    LGPLv2
Summary:    CLI that communicates with Koji to control building layered container images
Group:      Applications/System
%{?python_provide:%python_provide python2-%{name}-cli}
Provides:  koji-containerbuild-cli
Requires:   python2-koji >= 1.13

%description -n python2-%{name}-cli
Builder plugin that extend Koji to communicate with OpenShift build system and
build layered container images.

%if 0%{with python3}
%package -n python%{python3_pkgversion}-%{name}-cli
License:    LGPLv2
Summary:    CLI that communicates with Koji to control building layered container images
Group:      Applications/System
%{?python_provide:%python_provide python%{python3_pkgversion}-%{name}-cli}
Provides:  koji-containerbuild-cli
Requires:   python%{python3_pkgversion}-koji >= 1.13

%description -n python%{python3_pkgversion}-%{name}-cli
Builder plugin that extend Koji to communicate with OpenShift build system and
build layered container images.
%endif


%prep
%setup -q


%build
%if 0%{with python3}
%{__python3} setup.py build
%else
%{__python2} setup.py build
%endif


%install
rm -rf $RPM_BUILD_ROOT
%if 0%{with python3}
%{__python3} setup.py install -O1 --skip-build --root $RPM_BUILD_ROOT
%else
%{__python2} setup.py install -O1 --skip-build --root $RPM_BUILD_ROOT
%endif

%if 0%{with python3}
%{__install} -d $RPM_BUILD_ROOT%{_prefix}/lib/koji-hub-plugins
%{__install} -p -m 0644 %{module}/plugins/hub_containerbuild.py $RPM_BUILD_ROOT%{_prefix}/lib/koji-hub-plugins/hub_containerbuild.py
%{__install} -d $RPM_BUILD_ROOT%{_prefix}/lib/koji-builder-plugins
%{__install} -p -m 0644 %{module}/plugins/builder_containerbuild.py $RPM_BUILD_ROOT%{_prefix}/lib/koji-builder-plugins/builder_containerbuild.py
%{__install} -d $RPM_BUILD_ROOT%{python3_sitelib}/koji_cli_plugins
%{__install} -p -m 0644 %{module}/plugins/cli_containerbuild.py $RPM_BUILD_ROOT%{python3_sitelib}/koji_cli_plugins/cli_containerbuild.py
%else
%{__install} -d $RPM_BUILD_ROOT%{python2_sitelib}/koji_cli_plugins
%{__install} -p -m 0644 %{module}/plugins/cli_containerbuild.py $RPM_BUILD_ROOT%{python2_sitelib}/koji_cli_plugins/cli_containerbuild.py
%endif


%files
%if 0%{with python3}
%{python3_sitelib}/*
%else
%{python2_sitelib}/*
%endif
%doc docs AUTHORS README.rst
%if 0%{?rhel} && 0%{?rhel} <= 6
%{!?_licensedir:%global license %doc}
%endif
%license LICENSE

%if 0%{with python3}
%files -n python%{python3_pkgversion}-%{name}-cli
%{python3_sitelib}/koji_cli_plugins

%files hub
%{_prefix}/lib/koji-hub-plugins/hub_containerbuild.py*

%files builder
%{_prefix}/lib/koji-builder-plugins/builder_containerbuild.py*
%else
%files -n python2-%{name}-cli
%{python2_sitelib}/koji_cli_plugins
%endif


%clean
rm -rf $RPM_BUILD_ROOT


%changelog
* Wed May 17 2023 Robert Cerven <rcerven@redhat.com> 1.3.0-1
- new upstream release: 1.3.0

* Thu Dec 15 2022 Robert Cerven <rcerven@redhat.com> 1.2.1-1
- new upstream release: 1.2.1

* Wed Dec 14 2022 mkosiarc <mkosiarc@redhat.com> 1.2.0-1
- new upstream release: 1.2.0

* Thu Nov 03 2022 Robert Cerven <rcerven@redhat.com> 1.1.0-1
- new upstream release: 1.1.0

* Tue Oct 11 2022 Martin Basti <mbasti@redhat.com> 1.0.1-1
- new upstream release: 1.0.1

* Thu Oct 06 2022 rcerven <rcerven@redhat.com> 1.0.0-1
- new upstream release: 1.0.0

* Fri Jul 30 2021 mkosiarc <mkosiarc@redhat.com> 0.13.0-1
- builder: document signing_intent parameter (kdreyer@redhat.com)
- Freeze flexmock version to 0.10.4 (mbasti@redhat.com)
- Pin jsonschema==3.2.0 in requirements.txt (ben.alkov@redhat.com)

* Wed Jun 09 2021 Robert Cerven <rcerven@redhat.com> 0.12.0-1
- Factor out duplicated code for handling task response (mbasti@redhat.com)
- Refactoring: simplify creation of tasks results (mbasti@redhat.com)
- Test UW reading (ssalatsk@redhat.com)
- Add user warnings to the task output (ssalatsk@redhat.com)
- Provide an unit test for user warnings (ssalatsk@redhat.com)
- Collect user warnings from logs and store them (ssalatsk@redhat.com)
- Remove runtime assertions (ben.alkov@redhat.com)
- No more TODOs and FIXME in code (mbasti@redhat.com)
- Drop importlib_metadata from requirements.txt (miro@hroncok.cz)
- Fix: Fix passing operator_csv_modifications_url param to orchestrator
  (mbasti@redhat.com)
- Replace deprecated get-pip.py url (mkosiarc@redhat.com)

* Mon Mar 15 2021 Martin Bašti <mbasti@redhat.com> 0.11.0-1
- add task signatures to koji.task.LEGACY_SIGNATURES (tkopecek@redhat.com)
- New option --operator-csv-modifications-url (mbasti@redhat.com)
- Replace f32 with f33 to run bandit and pylint (cqi@redhat.com)
- Fix coveralls 422 Client Error (qcxhome@gmail.com)
- Replace f31 with f33 in CI workflow (qcxhome@gmail.com)

* Mon Jan 18 2021 Martin Bašti <mbasti@redhat.com> 0.10.0-1
- Sort CLI output alphabetically (mbasti@redhat.com)
- Pin 'bandit<1.6.3' to resolve an install error on Cent/Py2
  (ben.alkov@redhat.com)
- Add 'importlib_metadata' to requirements.txt and pin it < 3...
  (ben.alkov@redhat.com)
- Add pytest.ini for pytest improvements (ben.alkov@redhat.com)
- Silence remaining flake8 hits; mostly WS and wraps (ben.alkov@redhat.com)
- Add GH workflows (ben.alkov@redhat.com)
- Add new reqs for unit tests (ben.alkov@redhat.com)
- Refactor test.sh (ben.alkov@redhat.com)
- Update README badges (ben.alkov@redhat.com)
- Remove unneeded config files (ben.alkov@redhat.com)

* Fri Nov 06 2020 Robert Cerven <rcerven@redhat.com> 0.9.0-1
- Add unit test (ben.alkov@redhat.com)
- Trap OsbsValidationException, print it, and raise it as ContainerError
  (ben.alkov@redhat.com)
- 'pytest-capturelog' is dead and must be removed (ben.alkov@redhat.com)
- builder: document space delimier for arch_override (kdreyer@redhat.com)
- pyrsistent support for Py 2 ends with release 0.16 (ben.alkov@redhat.com)
- [markdown] Use older version of mdl (0.9) (mbasti@redhat.com)

* Wed Jul 29 2020 Robert Cerven <rcerven@redhat.com> 0.8.0-1
- Early check for missing branch, and scratch with isolated build conflict
  (rcerven@redhat.com)

* Fri Apr 24 2020 Martin Bašti <mbasti@redhat.com> 0.7.22-1
- Install koji-c and deps for pylint run (athos@redhat.com)
- Replace "Signed-off-by:" maintainer task with DCO... (ben.alkov@redhat.com)
- Add markdownlint config/rules (ben.alkov@redhat.com)
- Enable MD linting via Travis/test.sh (ben.alkov@redhat.com)
- Lint all Markdown files (ben.alkov@redhat.com)
- Mute other errors (ssalatsk@redhat.com)
- Mute 'redefined-builtin' warning (ssalatsk@redhat.com)
- Fix single errors (ssalatsk@redhat.com)
- Fix 'logging-not-lazy' (ssalatsk@redhat.com)
- Import 'absolute_import' (ssalatsk@redhat.com)
- Update travis CI for pylint testing (ssalatsk@redhat.com)
- Add pylint testing (ssalatsk@redhat.com)
- packit: don't propose downstream update (mbasti@redhat.com)
- Revert "Allow additional properties for containerbuild" (mlangsdo@redhat.com)
- fix: tito py2 support (mbasti@redhat.com)
- fix: update tito builder to match with expected value in %%setup
  (mastyk@redhat.com)
- Freeze python 2 test dependency verions (athos@redhat.com)
- fix: add custom builder for tito (mastyk@redhat.com)

* Tue Mar 31 2020 Martin Bašti <mbasti@redhat.com> 0.7.21-1
- tito: tag only with version (mbasti@redhat.com)
- specfile: remove unneded buildroot definition (mbasti@redhat.com)
- specfile: properly identify upstream source (mbasti@redhat.com)
- tito: use version tagger (mbasti@redhat.com)
- don't check for build existance with skip-build (rcerven@redhat.com)
- Change travis for working with Fedora 31 (sergsalatsky1703@gmail.com)
- Use podman as testing container engine (sergsalatsky1703@gmail.com)
- Simplify packit config (mbasti@redhat.com)

* Mon Feb 24 2020 Robert Cerven <rcerven@redhat.com> 0.7.20.1-1
- update setup version for 0.7.20.1 (rcerven@redhat.com)
- allow compose_ids used together with repo_url (rcerven@redhat.com)

* Tue Feb 18 2020 Robert Cerven <rcerven@redhat.com> 0.7.20-1
- update setup version for 0.7.20 (rcerven@redhat.com)
- Configure packit (mbasti@redhat.com)
- Handle new cachito dependency replacements argument (athos@redhat.com)
- Remove urlgrabber dependency (mbasti@redhat.com)

* Thu Jan 23 2020 Robert Cerven <rcerven@redhat.com> 0.7.19-1
- update setup version for 0.7.19 (rcerven@redhat.com)
- Handle whitelist annotation as a string (athos@redhat.com)
- Fix dependency error in test script (athos@redhat.com)
- Unit test to make sure additional properties are fine (twaugh@redhat.com)
- Automatic commit of package [koji-containerbuild] minor release [0.7.18.3-1].
  (rcerven@redhat.com)
- Allow additional properties for containerbuild (rcerven@redhat.com)
- Automatic commit of package [koji-containerbuild] minor release [0.7.18.2-1].
  (rcerven@redhat.com)
- Allow additional properties for containerbuild (rcerven@redhat.com)
- use orchestrator.log name for log in task for source container
  (rcerven@redhat.com)
- Automatic commit of package [koji-containerbuild] minor release [0.7.18.1-1].
  (rcerven@redhat.com)
- don't check for build nvr existance for autorebuilds which have
  'triggered_after_koji_task', (rcerven@redhat.com)
- Generate build_annotations.json file (athos@redhat.com)

* Wed Dec 18 2019 Robert Cerven <rcerven@redhat.com> 0.7.18.3-1
- Allow additional properties for containerbuild (rcerven@redhat.com)

* Wed Dec 18 2019 Robert Cerven <rcerven@redhat.com> 0.7.18.2-1
- Allow additional properties for containerbuild (rcerven@redhat.com)

* Thu Dec 12 2019 Robert Cerven <rcerven@redhat.com> 0.7.18.1-1
- don't check for build nvr existance for autorebuilds which have
  'triggered_after_koji_task', (rcerven@redhat.com)

* Tue Dec 10 2019 Robert Cerven <rcerven@redhat.com> 0.7.18-1
- Enable shellcheck (bash) lint (mbasti@redhat.com)
- Fail build for existing build if build state isn't failed or canceled, which
  will be refunded and reusable (rcerven@redhat.com)
- add custom userdata dictionary to args list (mlangsdo@redhat.com)
- builder: describe the purpose of git_branch parameter (kdreyer@redhat.com)

* Tue Dec 03 2019 Robert Cerven <rcerven@redhat.com> 0.7.17-1
- use extra keys for build type instead of new typeinfo which is just on new
  build (rcerven@redhat.com)
- use koji build 'name' instead of 'package_name' for source containers, for
  consistency with atomic-reator (rcerven@redhat.com)
- Disallow building source container from source container image build, and
  also disallow any other than 'image' build type (rcerven@redhat.com)
- Fix koji-build-id type (mbasti@redhat.com)
- source container build (rcerven@redhat.com)
- pass triggered_afer_koji_task to osbs-client even when it is 0
  (rcerven@redhat.com)

* Tue Nov 05 2019 Robert Cerven <rcerven@redhat.com> 0.7.16-1
- hub: improve docs for "buildContainer" RPC (kdreyer@redhat.com)
- README: correct path to Koji Hub configuration file (kdreyer@redhat.com)
- Add PR template (mbasti@redhat.com)
- Pass triggered_after_koji_task parameter through to osbs-client
  (rcerven@redhat.com)
- CI: centos 7: explicitly install more-itertools (mbasti@redhat.com)
- CI: install only py2 osbs-client dependencies in py2 tests
  (mbasti@redhat.com)

* Tue Sep 24 2019 Robert Cerven <rcerven@redhat.com> 0.7.15-1
- Skip build option, to update just buildconfig for autorebuilds
  (rcerven@redhat.com)
- check for required labels name & components only, allow other labels to be
  defined from env, check build existence only when explicitly defined nvr
  (rcerven@redhat.com)
- Stop requiring old pytest (acmiel@redhat.com)
- Fix tests for pytest version 5 (acmiel@redhat.com)
- Put jsonschema definition directly in python file (acmiel@redhat.com)
- README: Update CLI configuration section (mbasti@redhat.com)
- Declare python package runtime dependencies (athos@redhat.com)
- Add jsonschema validation for task options (acmiel@redhat.com)
- Clean up hub_containerbuild testing (acmiel@redhat.com)
- Install missing dependency in testing script (acmiel@redhat.com)
- Run Bandit static analyzer on CI jobs (athos@redhat.com)
- Remove Fedora 28 testing (acmiel@redhat.com)
- Enable python3 unit-testing (acmiel@redhat.com)
- Install koji-hub package during tests (acmiel@redhat.com)

* Wed Jun 12 2019 Robert Cerven <rcerven@redhat.com> 0.7.14-1
- fix building when only py3 is available in rhel8 (rcerven@redhat.com)
- enable building py3 package for rhel8 (rcerven@redhat.com)
- change organization references from release-engineering to
  containerbuildsystem (rcerven@redhat.com)
- Add unit tests for cli_containerbuild (acmiel@redhat.com)
- Rename test_kcb to test_builder_containerbuild (acmiel@redhat.com)
- Move existing CLI tests to their own file (acmiel@redhat.com)
- Fix wrong license in test_kcb.py (acmiel@redhat.com)
- Remove dead code (twaugh@redhat.com)
- Add unit tests for hub_containerbuild plugin (acmiel@redhat.com)
- Test methods for writings logs (acmiel@redhat.com)
- Test BuildContainerTask.createContainer() failures (acmiel@redhat.com)
- Test missing labels in BuildContainerTask.checkLabels() (acmiel@redhat.com)
- Test scratch configuration in BuildContainerTask.osbs() (acmiel@redhat.com)
- Add stickler config file (acmiel@redhat.com)

* Fri Mar 08 2019 Robert Cerven <rcerven@redhat.com> 0.7.13.1-1
- allow yum_repo with compose_id (rcerven@redhat.com)

* Wed Mar 06 2019 Robert Cerven <rcerven@redhat.com> 0.7.13-1
- Add coverall badge to Readme (mbasti@redhat.com)
- CI: Install coverall in install step (mbasti@redhat.com)
- containerbuild: make sure the target exists before trying to tag it
  (mlangsdo@redhat.com)
- use incremental_upload for metadata.json, because source file is on read-only
  filesystem (rcerven@redhat.com)

* Fri Jan 11 2019 Robert Cerven <rcerven@redhat.com> 0.7.12.1-1
- copy instead of move metadata.json, because brew builder has source mounted
  as read-only (rcerven@redhat.com)

* Tue Jan 08 2019 Robert Cerven <rcerven@redhat.com> 0.7.12-1
- move metadata.json to task output for scratch (rcerven@redhat.com)
- Add development requirements file (athos@redhat.com)
- Update OSBS Flatpak support check (athos@redhat.com)
- Add CONTRIBUTING.md (twaugh@redhat.com)
- README: updates for osbs-client package (kdreyer@redhat.com)
- Drop py2.6 support (mbasti@redhat.com)
- travis: use recent fedora versions (mbasti@redhat.com)
- Py3: replace `file` with `open` (mbasti@redhat.com)
- Py 2to3 updates (mbasti@redhat.com)

* Fri Oct 05 2018 Robert Cerven <rcerven@redhat.com> 0.7.11-1
- No need to use psql when creating channel (tkopecek@redhat.com)
- Proper obsolete of koji-containerbuild-cli (tkopecek@redhat.com)
- Automatic commit of package [koji-containerbuild] minor release [0.7.10-1].
  (rcerven@redhat.com)

* Wed Aug 22 2018 Robert Cerven <rcerven@redhat.com> 0.7.10-1
- Pin setuptools version for RHEL6 to 39.2 (twaugh@redhat.com)
- parse compose-ids as integers (rcerven@redhat.com)

* Fri Jun 29 2018 Robert Cerven <rcerven@redhat.com> 0.7.9.1-1
- allow arch-override for isolated builds (mlangsdo@redhat.com)

* Wed Jun 13 2018 Robert Cerven <rcerven@redhat.com> 0.7.9-1
- conditional install for py3 (tkopecek@redhat.com)
- typo in python3-koji require (tkopecek@redhat.com)
- BuildRequire for python3-devel (tkopecek@redhat.com)
- Fix CentOS 6 Travis CI tests (twaugh@redhat.com)
- make CLI proper koji plugin (tkopecek@redhat.com)
- test.sh: install PyYAML for osbs-client (twaugh@redhat.com)
- docs: minor improvements (lmeyer@redhat.com)

* Fri Mar 23 2018 Robert Cerven <rcerven@redhat.com> 0.7.8-1
- cleanup flatpak (rcerven@redhat.com)
- Use with-open-as instead of open-try-finally-close (twaugh@redhat.com)
- remove retries of reading logs (rcerven@redhat.com)

* Tue Jan 16 2018 Robert Cerven <rcerven@redhat.com> 0.7.7-2
- Disable rawhide testing (lucarval@redhat.com)
- Make Docker relabel the volume (lucarval@redhat.com)
- travis.yml: F25 is EOL, use F27 instead (vrutkovs@redhat.com)
- Disallow use of yum_repourls with compose_ids (bfontecc@redhat.com)
- tests: install specific versions of test package to avoid test packages
  update breaks (vrutkovs@redhat.com)
- Add compose_ids and signing_intent options (bfontecc@redhat.com)
- Disallow private branches for non-scratch container builds
  (bfontecc@redhat.com)

* Mon Nov 06 2017 Robert Cerven <rcerven@redhat.com> 0.7.6-2
- Added docstring to CLI (tkopecek@redhat.com)
- raise error when arches_override are used for non-scratch builds
  (rcerven@redhat.com)

* Wed Oct 04 2017 Robert Cerven <rcerven@redhat.com> 0.7.5-2
- test.sh: install epel-release for centos first (vrutkovs@redhat.com)
- Run tests in Travis CI using containers (vrutkovs@redhat.com)
- setup.py: remove bogus install_requires (vrutkovs@redhat.com)
- Remove pycurl (vrutkovs@redhat.com)
- Add FileHandler for osbs module (lucarval@redhat.com)
- Fix test_cli_args unit test (lucarval@redhat.com)
- Remove kojid module loading (lucarval@redhat.com)
- Don't check for isolated option if flatpak is specified (vrutkovs@redhat.com)
- Use arch-override as str (lucarval@redhat.com)
- test_kcb: initial infra for improved logging self-tests/verification
  (jarod@redhat.com)
- builder_containerbuild: clean up and simplify demux/non-demux split
  (jarod@redhat.com)
- builder_containerbuild: write cleanups (jarod@redhat.com)
- builder_containerbuild: use get_orchestrator_build_logs api
  (jarod@redhat.com)
- Flatpak support (otaylor@fishsoup.net)
- Clean up container-build CLI (otaylor@fishsoup.net)
- cli: add --arches argument option (mlangsdo@redhat.com)
- cli: write a test case for the arguments parser (mlangsdo@redhat.com)
- tests: throw OsbsOrchestratorNotEnabled if available (otaylor@fishsoup.net)

* Tue Sep 05 2017 Robert Cerven <rcerven@redhat.com> 0.7.4-1
- Use OsbsOrchestratorNotEnabled exception (lucarval@redhat.com)
- Add support for isolated builds (lucarval@redhat.com)

* Mon Jul 31 2017 Robert Cerven <rcerven@redhat.com> 0.7.3-1
- Fix typo, koij -> koji (vrutkovs@redhat.com)
- Update API for koji 1.13 changes:  * activate_session now requires two
  parameters  * _running_in_bg moved to koji_cli.lib (vrutkovs@redhat.com)
- fix tests, call correct koji api to cancel task
  (maxamillion@fedoraproject.org)
- add speculative message about osbs cancellation for autorebuild
  (maxamillion@fedoraproject.org)
- Cancel a koji task if the osbs task is cancelled
  (maxamillion@fedoraproject.org)

* Tue Apr 04 2017 Robert Cerven <rcerven@redhat.com> 0.7.2-1
- check if pkg can be tagged (lucarval@redhat.com)
- orchestrator build can only successfully run if can_orchestrate option is
  set, in osbs.conf otherwise will fallback to regular prod_build
  (rcerven@redhat.com)
- add unit testing for orchestrator builds (lucarval@redhat.com)
- Use osbs-client's create_orchestrator_build method if available
  (vrutkovs@redhat.com)

* Tue Nov 15 2016 Luiz Carvalho <lucarval@redhat.com> 0.7.1-1
- Move long tag check in checkLabels (vrutkovs@redhat.com)
- Update tests and documentation after tasks have been merged
  (vrutkovs@redhat.com)
- Merge buildContainer and createContainer tasks (vrutkovs@redhat.com)
- Pick the longest tag correctly (vrutkovs@redhat.com)
- Pick the longest tag part among release and a list from additional-tags
  (vrutkovs@redhat.com)
- Reject a build if the longest tag is longer than 128 chars
  (vrutkovs@redhat.com)
- Trap build cancellation using signal handler (vrutkovs@redhat.com)
- tests: make tests work (twaugh@redhat.com)
- Cancel build when createContainer task is cancelled (throws
  koji.GenericException) (vrutkovs@redhat.com)
- Drop support for release parameter (lucarval@redhat.com)

* Wed Jun 29 2016 Luiz Carvalho <lucarval@redhat.com> 0.6.6-1
- Use osbs 'scratch' configuration for scratch builds (vrutkovs@redhat.com)
- Revert "Disable scratch builds" (vrutkovs@redhat.com)
- make streamed logs line-buffered (twaugh@redhat.com)
- Remove default value for release parameter (lucarval@redhat.com)

* Fri Jun 24 2016 Luiz Carvalho <lucarval@redhat.com> 0.6.5-1
- Show build's error message if available (vrutkovs@redhat.com)
- Disable scratch builds (vrutkovs@redhat.com)
- Should use label 'Release' to set release (twaugh@redhat.com)

* Wed Jun 22 2016 Brendan Reilly <breilly@redhat.com> 0.6.4-1
- Fail createContainer if OpenShift build fails (mmilata@redhat.com)
- Added a few unit tests (breilly@redhat.com)
- Make "Release" label optional (lucarval@redhat.com)
- Use correct object for session during nvr check and log the error
  (vrutkovs@redhat.com)

* Thu Jun 02 2016 Brendan Reilly <breilly@redhat.com> 0.6.3-1
- Fix task result output (lucarval@redhat.com)
- Handle release parameter (lucarval@redhat.com)

* Wed May 25 2016 Brendan Reilly <breilly@redhat.com> 0.6.2-1
- supply koji_task_id to osbs-client's create_build() (twaugh@redhat.com)
- no need to warn about build result not being JSON (twaugh@redhat.com)
- Use component label in nvr check (vrutkovs@redhat.com)
- Don't check NVR for scratch builds and move nvr check closer to build object
  creation (vrutkovs@redhat.com)
- Don't start the build if package with this NVR already has been built
  (vrutkovs@redhat.com)
- Expose Koji CG build ID in CreateContainerTask (lucarval@redhat.com)

* Mon Apr 11 2016 Brendan Reilly <breilly@redhat.com> 0.6.1-1
- Reinstate _get_repositories() method (fixes #35) (twaugh@redhat.com)
- Add back in bits required for streaming logs (fixes #33) (twaugh@redhat.com)

* Thu Apr 07 2016 Brendan Reilly <breilly@redhat.com> 0.6.0-1
- remove un-necessary code for v2-only CG builds
  (maxamillion@fedoraproject.org)
- runBuilds: add debug for arches (dennis@ausil.us)
- runBuilds make label unique and be able to build archfully (dennis@ausil.us)
- Build process documentation - quick and dirty (pbabinca@redhat.com)

* Mon Mar 14 2016 Pavol Babincak <pbabinca@redhat.com> 0.5.7-1
- Updated docs how to create a release (pbabinca@redhat.com)
- add some post-install instructions (admiller@redhat.com)
- incorporated new osbs api for compression fix (breilly@redhat.com)

* Tue Mar 08 2016 Pavol Babincak <pbabinca@redhat.com> 0.5.6-1
- Backport spec file from Fedora (pbabinca@redhat.com)
- Include docs in MANIFEST.in (pbabinca@redhat.com)
- Use .md extension for build architecture (pbabinca@redhat.com)
- quickfix for downloads always being .tar (breilly@redhat.com)
- Channel override in CLI (pbabinca@redhat.com)
- Build process documentation - quick and dirty (pbabinca@redhat.com)

* Thu Feb 04 2016 Fedora Release Engineering <releng@fedoraproject.org> - 0.5.5-2
- Rebuilt for https://fedoraproject.org/wiki/Fedora_24_Mass_Rebuild

* Fri Dec 04 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.5-1
- Add README.rst to a release (pbabinca@redhat.com)
- Use %%global macro instead of %%define one (pbabinca@redhat.com)
- Require main package in subpackages to always install license file
  (pbabinca@redhat.com)
- Add license directives to subpackages (pbabinca@redhat.com)

* Thu Dec 03 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.4-3
- Simplify inclusion of python modules to get proper owners
  (pbabinca@redhat.com)
- Explicit __python2 definitions on <=rhel6 (pbabinca@redhat.com)
- Explicit use of python2 and BuildRequires on python2-devel
  (pbabinca@redhat.com)
- %%defattr macro isn't needed anymore (pbabinca@redhat.com)
- Use %%license tag for license on RHEL && RHEL <= 6 (pbabinca@redhat.com)
- Fix permissions for CLI binary (pbabinca@redhat.com)
- Wrap package descriptions to make rpmlint happy (pbabinca@redhat.com)
- Replace Requires on osbs with osbs-client (pbabinca@redhat.com)
- Remove koji Requires from the base package (pbabinca@redhat.com)
- Replace koji-builder with koji dependency for cli subpackage
  (pbabinca@redhat.com)
- Specify how release tarballs are created (pbabinca@redhat.com)
- Use build system instead of buildsystem to make rpmlint happy
  (pbabinca@redhat.com)
- Fix name macro in URL (pbabinca@redhat.com)

* Fri Nov 20 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.4-2
- fix spec paths, libdir evals to /usr/lib64/ on 64-bit build hosts which is
  the wrong path for koji plugins (admiller@redhat.com)

* Fri Nov 20 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.4-1
- Reinit curl after fork to properly process incremental logs
  (pbabinca@redhat.com)
- Add support to new LABEL names and make architecture optional
  (pbabinca@redhat.com)
- Fix serious issue: check external rpms for *non*scratch builds
  (pbabinca@redhat.com)
- Catch errors raised by markExternalRPMs and raise it as koji.PostBuildError
  (pbabinca@redhat.com)
- Get list of rpms and repositories only for successful builds
  (pbabinca@redhat.com)
- Download image tarball only if build was successful (pbabinca@redhat.com)
- Log list of all rpms from osbs response as formatted rpm list
  (pbabinca@redhat.com)
- Refactor: get rpm packages to separate method (pbabinca@redhat.com)
- Refactor: get docker repositories to separate method (pbabinca@redhat.com)
- Fail only if build was successful and it haven't generated any tarball
  (pbabinca@redhat.com)
- Improve log write related exception messages (pbabinca@redhat.com)
- Raise ContainerError exceptions when something goes wrong with osbs logs
  (pbabinca@redhat.com)
- Pass branch and push_url from opts to osbs's create_build()
  (pbabinca@redhat.com)
- Uploader process check if child (which fetches logs) finished
  (pbabinca@redhat.com)
- Overall docs about build architecture (pbabinca@redhat.com)
- change log msg level to info (mikem@redhat.com)
- Properly handle empty repositories in osbs response (pbabinca@redhat.com)
- Wait between new connection/fetch logs (pbabinca@redhat.com)
- Use get_build_name() instead of build_id to get osbs build id
  (pbabinca@redhat.com)

* Tue Jul 14 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.3-1
- List repositories in status message of buildContainer task
  (pbabinca@redhat.com)
- Print osbs build id in the error message about failed build
  (pbabinca@redhat.com)
- If not exactly one image was built leave fail to parent (pbabinca@redhat.com)
- Use DockerfileParser class from dockerfile_parse module for parsing
  (pbabinca@redhat.com)
- Download docker logs at the end of the build (pbabinca@redhat.com)
- Try fetch OSBS logs with follow and incrementally upload them
  (pbabinca@redhat.com)
- If final tarball cannot be downloaded log error and continue
  (pbabinca@redhat.com)
- Accept repo URLs in CLI and pass it in builder plugin to osbs
  (pbabinca@redhat.com)
- Improve error message when there were unexpected number of builds
  (pbabinca@redhat.com)
- Fix: correctly format string before passing to ContainerError
  (pbabinca@redhat.com)
- Fix formatting of README.rst (pbabinca@redhat.com)

* Mon Jun 15 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.2-1
- Use BZComponent LABEL instead of Name (pbabinca@redhat.com)

* Fri Jun 12 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.1-1
- Explicit string conversion before urlgrabber.urlgrab() and more logging
  (pbabinca@redhat.com)
- Explicitly set urlgrab ssl verify options which pycurl expects
  (pbabinca@redhat.com)

* Fri Jun 12 2015 Pavol Babincak <pbabinca@redhat.com> 0.5.0-1
- Read LABELs from Dockerfile (pbabinca@redhat.com)

* Fri Jun 12 2015 Pavol Babincak <pbabinca@redhat.com> 0.4.0-1
- Download container image via https (pbabinca@redhat.com)
- Tag package (image) after successful build if not scratch
  (pbabinca@redhat.com)

* Tue Jun 09 2015 Pavol Babincak <pbabinca@redhat.com> 0.3.1-1
- Add missing import imp (pbabinca@redhat.com)

* Mon Jun 08 2015 Pavol Babincak <pbabinca@redhat.com> 0.3.0-1
- Remove code which always overwrote release (pbabinca@redhat.com)
- Removed not used imports (pbabinca@redhat.com)
- Import kojipath from path set via variable not from inspection
  (pbabinca@redhat.com)
- More debug info: list rpm_packages (pbabinca@redhat.com)
- Mock image tarball as we don't get this from the buildsystem (yet)
  (pbabinca@redhat.com)
- Pull getting task options to separate method (pbabinca@redhat.com)
- Pull package (image) whitelist check into separate method
  (pbabinca@redhat.com)
- Reuse image tables and methods for container builds (pbabinca@redhat.com)
- Don't pass build_tag as separate argument to createContainer task
  (pbabinca@redhat.com)

* Wed Jun 03 2015 Pavol Babincak <pbabinca@redhat.com> 0.2.0-2
- Don't require python-distutils. distutils is part of python-libs pkg
  (pbabinca@redhat.com)

* Wed May 27 2015 Pavol Babincak <pbabinca@redhat.com> 0.2.0-1
- Explicitly list code which are hack around database constraints
  (pbabinca@redhat.com)
- refactor: remove not used code and move comment to better position
  (pbabinca@redhat.com)
- Get name from name of the basename repository for non-scratch builds
  (pbabinca@redhat.com)
- Extend SCM object with get_component() and get_git_uri() and use it
  (pbabinca@redhat.com)
- Use logger to write logs and not sys.stderr.write (pbabinca@redhat.com)
- Use container_archives not image_archives table (pbabinca@redhat.com)
- Use attributes of BuildResponse object to query responses
  (pbabinca@redhat.com)
- Connect to osbs logger to print more debug info via own logger
  (pbabinca@redhat.com)
- Improve rpm_packages listings (pbabinca@redhat.com)
- Support non-scratch builds with listing of the contents (pbabinca@redhat.com)
- builderplugin: import kojid binary as kojid module (pbabinca@redhat.com)
- builderplugin: Use single handler to OSBS object (pbabinca@redhat.com)

* Mon May 18 2015 Pavol Babincak <pbabinca@redhat.com> 0.1.2-1
- add BuildRoot tag (needed for rhel<6) (mikem@redhat.com)
- use alternate method to import kojihub (mikem@redhat.com)

* Wed May 13 2015 Pavol Babincak <pbabinca@redhat.com> 0.1.1-1
- Documentation for buildContainer task (pbabinca@redhat.com)
- In buildContainer task use "container" channel by default
  (pbabinca@redhat.com)

* Wed May 13 2015 Pavol Babincak <pbabinca@redhat.com> 0.1.0-2
- Bump Release instead of Version (pbabinca@redhat.com)
- Use BuildArch noarch (pbabinca@redhat.com)

* Mon May 04 2015 Pavol Babincak <pbabinca@redhat.com> 0.1.0-1
- first public release
