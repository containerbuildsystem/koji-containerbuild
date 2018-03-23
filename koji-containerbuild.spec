%if 0%{?rhel} && 0%{?rhel} <= 6
%{!?__python2: %global __python2 /usr/bin/python2}
%{!?python2_sitelib: %global python2_sitelib %(%{__python2} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}
%endif

%global module koji_containerbuild

Name:           koji-containerbuild
Version:        0.7.8
Release:        1%{?dist}
Summary:        Koji support for building layered container images
Group:          Applications/System

License:        LGPLv2
URL:            https://github.com/release-engineering/%{name}
#Use the following commands to generate the tarball:
#
#    git clone --single-branch --branch koji-containerbuild-VERSION-RELEASE \
#      https://github.com/release-engineering/koji-containerbuild.git && \
#    cd ./koji-containerbuild && \
#    python setup.py sdist
#
#Where:
#- VERSION is version macro from specfile
#- RELEASE is release macro from specfile and dist is not defined (empty)
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRoot:      %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)

BuildRequires:  python
BuildRequires:  python2-devel
BuildRequires:  python-setuptools

%description
Koji support for building layered container images


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
Requires:   koji-builder
Requires:   koji-containerbuild
Requires:   osbs-client
Requires:   python-urlgrabber
Requires:   python-dockerfile-parse

%description builder
Builder plugin that extend Koji to communicate with OpenShift build system and
build layered container images.


%package cli
License:    LGPLv2
Summary:    CLI that communicates with Koji to control building layered container images
Group:      Applications/System
Requires:   koji
Requires:   koji-containerbuild

%description cli
Builder plugin that extend Koji to communicate with OpenShift build system and
build layered container images.

%prep
%setup -q


%build
%{__python2} setup.py build


%install
rm -rf $RPM_BUILD_ROOT
%{__python2} setup.py install -O1 --skip-build --root $RPM_BUILD_ROOT
%{__install} -d $RPM_BUILD_ROOT%{_bindir}
%{__install} -p -m 0755 cli/koji-containerbuild $RPM_BUILD_ROOT%{_bindir}/koji-containerbuild
%{__install} -d $RPM_BUILD_ROOT%{_prefix}/lib/koji-hub-plugins
%{__install} -p -m 0644 %{module}/plugins/hub_containerbuild.py $RPM_BUILD_ROOT%{_prefix}/lib/koji-hub-plugins/hub_containerbuild.py
%{__install} -d $RPM_BUILD_ROOT%{_prefix}/lib/koji-builder-plugins
%{__install} -p -m 0644 %{module}/plugins/builder_containerbuild.py $RPM_BUILD_ROOT%{_prefix}/lib/koji-builder-plugins/builder_containerbuild.py


%files
%{python2_sitelib}/*
%doc docs AUTHORS README.rst
%if 0%{?rhel} && 0%{?rhel} <= 6
%{!?_licensedir:%global license %doc}
%endif
%license LICENSE

%files cli
%{_bindir}/*

%files hub
%{_prefix}/lib/koji-hub-plugins/hub_containerbuild.py*

%files builder
%{_prefix}/lib/koji-builder-plugins/builder_containerbuild.py*

%clean
rm -rf $RPM_BUILD_ROOT


%changelog
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
