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

`osbs <https://github.com/DBuildService/osbs>`_ package is required. In Fedora it
is part of official repositories. Additionally you'll need to modify
`/etc/osbs.conf` with addresses to OpenShift buildystem instance and registry.
Follow osbs documentation if you find any.

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

As the kojiadmin user (normally the koji system user), add the container channel to koji

::

    $ psql
    psql (8.4.20)
    Type "help" for help.

    koji=# INSERT INTO channels (name) VALUES ('container');

As the kojiadmin, add builder(s) to the channel and add a package

::

    $ koji add-host-to-channel kojibuilder1 container
    $ koji add-pkg --owner some_koji_user some_koji_target testing



