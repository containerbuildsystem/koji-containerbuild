%{!?python_sitelib: %define python_sitelib %(%{__python} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}
%{!?python_sitelib: %define python_sitelib %(%{__python} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}

%define module koji_containerbuild

Name:           koji-containerbuild
Version:        0.1.0
Release:        1%{?dist}
Summary:        Koji support for building layered container images
Group:          Applications/System

License:        LGPLv2
URL:            https://github.com/release-engineering/${name}
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python
BuildRequires:  python-setuptools
Requires:       koji

%description
Koji support for building layered container images


%package hub
Summary:    Hub plugin that extend Koji to build layered container images
Group:      Applications/System
Requires:   koji-hub

%description hub
Hub plugin that extend Koji to support building layered container images


%package builder
Summary:    Builder plugin that extend Koji to build layered container images
Group:      Applications/System
Requires:   koji-builder
Requires:   osbs

%description builder
Builder plugin that extend Koji to communicate with OpenShift buildsystem and build layered container images.


%package cli
Summary:    CLI that communicates with Koji to control building layered container images
Group:      Applications/System
Requires:   koji-builder
Requires:   python-distutils

%description cli
Builder plugin that extend Koji to communicate with OpenShift buildsystem and build layered container images.

%prep
%setup -q


%build
%{__python} setup.py build


%install
rm -rf $RPM_BUILD_ROOT
%{__python} setup.py install -O1 --skip-build --root $RPM_BUILD_ROOT
%{__install} -d $RPM_BUILD_ROOT%{_bindir}
%{__install} -p -m 0775 cli/koji-containerbuild $RPM_BUILD_ROOT%{_bindir}/koji-containerbuild
%{__install} -d $RPM_BUILD_ROOT%{_libdir}/koji-hub-plugins
%{__install} -p -m 0644 %{module}/plugins/hub_containerbuild.py $RPM_BUILD_ROOT%{_libdir}/koji-hub-plugins/hub_containerbuild.py
%{__install} -d $RPM_BUILD_ROOT%{_libdir}/koji-builder-plugins
%{__install} -p -m 0644 %{module}/plugins/builder_containerbuild.py $RPM_BUILD_ROOT%{_libdir}/koji-builder-plugins/builder_containerbuild.py


%files
%defattr(-,root,root)
%{python_sitelib}/%{module}/*
%{python_sitelib}/%{module}-*.egg-info
%doc docs AUTHORS LICENSE

%files cli
%defattr(-,root,root)
%{_bindir}/*

%files hub
%defattr(-,root,root)
%{_libdir}/koji-hub-plugins/hub_containerbuild.py*

%files builder
%defattr(-,root,root)
%{_libdir}/koji-builder-plugins/builder_containerbuild.py*

%clean
rm -rf $RPM_BUILD_ROOT


%changelog
* Mon May 04 2015 Pavol Babincak <pbabinca@redhat.com> 0.1.0-1
- first public release
