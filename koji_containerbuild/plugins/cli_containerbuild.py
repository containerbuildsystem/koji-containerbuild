"""Module intended to be put into koji CLI to get container build commands"""

# Copyright (C) 2015, 2019  Red Hat, Inc.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
# USA

# Authors:
#       Pavol Babincak <pbabinca@redhat.com>

from __future__ import absolute_import, print_function

import json

from koji.plugin import export_cli
from koji_cli.lib import _, activate_session, parse_arches, \
                         OptionParser, watch_tasks, _running_in_bg

# matches hub's buildContainer parameter channel
DEFAULT_CHANNEL = 'container'


def print_value(value, level, indent, suffix=''):
    offset = ' ' * level * indent
    print(''.join([offset, str(value), suffix]))


def print_result(result, level=0, indent=2):
    if isinstance(result, list):
        for item in result:
            print_result(item, level+1)
    elif isinstance(result, dict):
        for key, value in result.items():
            print_value(key, level, indent, ':')
            print_result(value, level+1)
    else:
        print_value(result, level, indent)


def print_task_result(task_id, result, weburl):
    try:
        result["koji_builds"] = ["%s/buildinfo?buildID=%s" % (weburl, build_id)
                                 for build_id in result.get("koji_builds", [])]
    except TypeError:
        pass

    print("Task Result (%s):" % task_id)
    print_result(result)


def parse_arguments(options, args, flatpak):
    "Build a container"
    if flatpak:
        usage = _("usage: %prog flatpak-build [options] target <scm url>")
    else:
        usage = _("usage: %prog container-build [options] target <scm url or "
                  "archive path>")
    usage += _("\n(Specify the --help global option for a list of other help "
               "options)")
    parser = OptionParser(usage=usage)
    parser.add_option("--scratch", action="store_true",
                      help=_("Perform a scratch build"))
    if not flatpak:
        parser.add_option("--isolated", action="store_true",
                          help=_("Perform an isolated build"))
    parser.add_option("--arch-override",
                      help=_("Requires --scratch or --isolated. Limit a build to "
                             "the specified arches. Comma or space separated."))
    parser.add_option("--wait", action="store_true",
                      help=_("Wait on the build, even if running in the "
                             "background"))
    parser.add_option("--nowait", action="store_false", dest="wait",
                      help=_("Don't wait on build"))
    parser.add_option("--quiet", action="store_true",
                      help=_("Do not print the task information"),
                      default=options.quiet)
    parser.add_option("--background", action="store_true",
                      help=_("Run the build at a lower priority"))
    parser.add_option("--replace-dependency", dest='dependency_replacements',
                      metavar="pkg_manager:name:version[:new_name]", action='append',
                      help=_("Cachito dependency replacement. May be used multiple times."))
    parser.add_option("--repo-url", dest='yum_repourls', metavar="REPO_URL",
                      action='append',
                      help=_("URL of yum repo file. May be used multiple times."))
    parser.add_option("--git-branch", metavar="GIT_BRANCH",
                      help=_("Git branch"))
    parser.add_option("--channel-override",
                      help=_("Use a non-standard channel [default: %default]"),
                      default=DEFAULT_CHANNEL)
    parser.add_option("--signing-intent",
                      help=_("Signing intent of the ODCS composes [default: %default]."
                             " Cannot be used with --compose-id"),
                      default=None, dest='signing_intent')
    parser.add_option("--compose-id",
                      help=_("ODCS composes used. May be used multiple times. Cannot be"
                             " used with --signing-intent"),
                      dest='compose_ids', action='append', metavar="COMPOSE_ID", type="int")
    parser.add_option("--skip-build", action="store_true",
                      help=_("Skip build and update buildconfig. "
                             "Use this option to update autorebuild settings"))
    parser.add_option("--userdata",
                      help=_("JSON dictionary of user defined custom metadata"))
    if not flatpak:
        parser.add_option("--release",
                          help=_("Set release value"))
        parser.add_option("--koji-parent-build",
                          help=_("Overwrite parent image with image from koji build"))
    build_opts, args = parser.parse_args(args)
    if len(args) != 2:
        parser.error(_("Exactly two arguments (a build target and a SCM URL) "
                       "are required"))
        assert False

    source = args[1]
    if '://' not in source:
        parser.error(_("scm URL does not look like an URL to a source repository"))
    if '#' not in source:
        parser.error(_("scm URL must be of the form <url_to_repository>#<revision>)"))

    if build_opts.arch_override and not (build_opts.scratch or build_opts.isolated):
        parser.error(_("--arch-override is only allowed for --scratch or --isolated builds"))

    if build_opts.signing_intent and build_opts.compose_ids:
        parser.error(_("--signing-intent cannot be used with --compose-id"))

    opts = {}
    if not build_opts.git_branch:
        parser.error(_("git-branch must be specified"))

    keys = ('scratch', 'yum_repourls', 'git_branch', 'signing_intent', 'compose_ids', 'skip_build',
            'userdata', 'dependency_replacements')

    if flatpak:
        opts['flatpak'] = True
    else:
        if build_opts.isolated and build_opts.scratch:
            parser.error(_("Build cannot be both isolated and scratch"))

        keys += ('release', 'isolated', 'koji_parent_build')

    if build_opts.arch_override:
        opts['arch_override'] = parse_arches(build_opts.arch_override)

    for key in keys:
        val = getattr(build_opts, key)
        if val is not None:
            opts[key] = val
            if key == 'userdata':
                opts[key] = json.loads(val)

    # create the parser in this function and return it to
    # simplify the unit test cases
    return build_opts, args, opts, parser


def parse_source_arguments(options, args):
    "Build a source container"
    usage = _("usage: %prog source-container-build [options] target")
    usage += _("\n(Specify the --help global option for a list of other help "
               "options)")
    parser = OptionParser(usage=usage)
    parser.add_option("--scratch", action="store_true",
                      help=_("Perform a scratch build"))
    parser.add_option("--wait", action="store_true",
                      help=_("Wait on the build, even if running in the "
                             "background"))
    parser.add_option("--nowait", action="store_false", dest="wait",
                      help=_("Don't wait on build"))
    parser.add_option("--quiet", action="store_true",
                      help=_("Do not print the task information"),
                      default=options.quiet)
    parser.add_option("--background", action="store_true",
                      help=_("Run the build at a lower priority"))
    parser.add_option("--channel-override",
                      help=_("Use a non-standard channel [default: %default]"),
                      default=DEFAULT_CHANNEL)
    parser.add_option("--signing-intent",
                      help=_("Signing intent of the ODCS composes [default: %default]."),
                      default=None, dest='signing_intent')
    parser.add_option("--koji-build-id",
                      type="int",
                      help=_("Koji build id for sources, "
                             "is required or koji-build-nvr is provided"))
    parser.add_option("--koji-build-nvr",
                      help=_("Koji build nvr for sources, "
                             "is required or koji-build-id is provided"))

    build_opts, args = parser.parse_args(args)

    if len(args) != 1:
        parser.error(_("Exactly one argument (a build target) is required"))
        assert False

    if not (build_opts.koji_build_id or build_opts.koji_build_nvr):
        parser.error(_("at least one of --koji-build-id and --koji-build-nvr has to be specified"))

    opts = {}
    keys = ('scratch', 'signing_intent', 'koji_build_id', 'koji_build_nvr')

    for key in keys:
        val = getattr(build_opts, key)
        if val is not None:
            opts[key] = val
    # create the parser in this function and return it to
    # simplify the unit test cases
    return build_opts, args, opts, parser


def handle_build(options, session, args, flatpak=False, sourcebuild=False):
    if sourcebuild:
        build_opts, args, opts, parser = parse_source_arguments(options, args)
    else:
        build_opts, args, opts, parser = parse_arguments(options, args, flatpak)

    activate_session(session, options)

    target = args[0]
    build_target = session.getBuildTarget(target)
    if not build_target:
        parser.error(_("Unknown build target: %s" % target))
    dest_tag = session.getTag(build_target['dest_tag'])
    if not dest_tag:
        parser.error(_("Unknown destination tag: %s" %
                       build_target['dest_tag_name']))
    if dest_tag['locked'] and not build_opts.scratch:
        parser.error(_("Destination tag %s is locked" % dest_tag['name']))

    priority = None
    if build_opts.background:
        # relative to koji.PRIO_DEFAULT
        priority = 5

    if sourcebuild:
        task_id = session.buildSourceContainer(target, opts, priority=priority,
                                               channel=build_opts.channel_override)
    else:
        source = args[1]
        task_id = session.buildContainer(source, target, opts, priority=priority,
                                         channel=build_opts.channel_override)

    if not build_opts.quiet:
        print("Created task: %s" % task_id)
        print("Task info: %s/taskinfo?taskID=%s" % (options.weburl, task_id))
    if build_opts.wait or (build_opts.wait is None and not _running_in_bg()):
        session.logout()
        rv = watch_tasks(session, [task_id], quiet=build_opts.quiet)

        # Task completed and a result should be available.
        if rv == 0:
            result = session.getTaskResult(task_id)
            print_task_result(task_id, result, options.weburl)

        return rv
    else:
        return


@export_cli
def handle_container_build(options, session, args):
    "[build] Build a container"
    return handle_build(options, session, args)


@export_cli
def handle_flatpak_build(options, session, args):
    "[build] Build a flatpak"
    return handle_build(options, session, args, flatpak=True)


@export_cli
def handle_source_container_build(options, session, args):
    "[build] Build a sourcecontainer"
    return handle_build(options, session, args, sourcebuild=True)
