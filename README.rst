Koji containerbuild
===================

This package extends Koji buildsystem with plugin which allows building
containers via OpenShift buildsystem. Additionally it provides CLI tool to
submit builds based on koji CLI.

Most likely you'll need to run own OpenShift instance. See OpenShift
documentation how to setup that.

Build a package
---------------

To build the current release, use the following command in the repo directory::

  tito build --rpm

Create release tarball
----------------------

To create tarball for a release run::

  python setup.py sdist

Create new release
------------------

Create upstream release
~~~~~~~~~~~~~~~~~~~~~~~

In this upstream repository:

1. Bump release and commit changelog:

    * If this is downstream update, run::

        tito tag

    * If this is upstream update you need to specify version. It needs to be without release part, only dot separated one (for example 0.5.7)::

        tito tag --use-version 0.5.7

2. See last line of tito output which give you a hint how to push commit and tag to remote (for example 0.5.7-1)::

    git push origin
    git push origin koji-containerbuild-0.5.7-1


3. Create srpm::

    tito build --srpm

Update downstream release
~~~~~~~~~~~~~~~~~~~~~~~~~
Following steps are for updating packages in Fedora:

1. Clone or pull latest changes in downstream repository::

    fedpkg co koji-containerbuild

2. Switch to branch which you want to update (e.g. start with master)::

    fedpkg switch-branch master

3. Import srpm created in previos section (for example koji-containerbuild version 0.5.7-1, updating Fedora 24)::

    fedpkg import /tmp/tito/koji-containerbuild-0.5.7-1.f24.src.rpm

  Review changes - there should be single new tarball, entries in changelog looks fine (e.g. not empty).

4. Try to build package as scratch-build::

    fedpkg scratch-build --srpm

5. If build succeeded commit changes to remote and build regular build::

    fedpkg push
    fedpkg build

Update other branches either by merging (preferred) or importing srpm.

Plugin installation
-------------------

Koji hub
~~~~~~~~

If you already use any Koji hub plugin you need to use same path for this
plugin too. Default path used by Koji hub is `/usr/lib/koji-hub-plugins`.

Modify `/etc/koji-hub.conf`:

* set `PluginPath` to a directory which contains `hub_containerbuild.py` from this
  package.

* add `hub_containerbuild` value to `Plugins`. If you have already some plugin
  enabled use space as a separator between names.

Finally (graceful) restart httpd daemon.

Koji builder
~~~~~~~~~~~~

The Koji builder plugin requires the `osbs-client
<https://github.com/projectatomic/osbs-client>`_ package. In Fedora it is part
of the official repositories. Additionally you'll need to modify
`/etc/osbs.conf` with the addresses to your OpenShift buildystem instance and
registry. Follow the `osbs documentation <https://osbs.readthedocs.io/>`_.

Similarly to Koji hub you'll need to find out which path will be used for
plugins. Default path used by Koji builder is `/usr/lib/koji-builder-plugins`.

* set `PluginPath` to a directory which contains `builder_containerbuild.py` from
  this package.

* add `builder_containerbuild` value to `Plugins`. Similarly to Koji hub use space
  to separate existing plugin names.

Koji CLI
~~~~~~~~

Package provides CLI binary with interface similar to upstream koji CLI. It
adds only single new command - `container-build` which allows submitting container
builds to Koji hub. To configure CLI you'll need to copy `[koji]` section in
`/etc/koji.conf` to `[koji-containerbuild]` and optionally adapt configuration
there.


Post Install Configuration
--------------------------

As the kojiadmin, add builder(s) to the newly created channel and add a
package

::

    $ koji add-host-to-channel --new kojibuilder1 container
    $ koji add-pkg --owner some_koji_user some_koji_target testing



