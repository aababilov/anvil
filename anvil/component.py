# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import pkg_resources
import re
import weakref

from anvil import colorizer
from anvil import constants
from anvil import date
from anvil import downloader as down
from anvil import exceptions as excp
from anvil import importer
from anvil import log as logging
from anvil import packager
from anvil import pip
from anvil import shell as sh
from anvil import trace as tr
from anvil import utils


LOG = logging.getLogger(__name__)


class ComponentBase(object):
    def __init__(self,
                 subsystems,
                 runner,
                 instances,
                 options,
                 name,
                 siblings,
                 *args,
                 **kargs):

        self.subsystems = subsystems
        self.name = name
        self.options = options

        # All the other active instances
        self.instances = instances

        # All the other class names that can be used alongside this class
        self.siblings = siblings

        # The runner has a reference to us, so use a weakref here to
        # avoid breaking garbage collection.
        self.runner = weakref.proxy(runner)

        # Parts of the global runner context that we use
        self.cfg = runner.cfg
        self.distro = runner.distro

        # Turned on and off as phases get activated
        self.activated = False

    def get_option(self, opt_name, def_val=None):
        return self.options.get(opt_name, def_val)

    def verify(self):
        # Ensure subsystems are known...
        knowns = self.known_subsystems()
        for s in self.subsystems:
            if s not in knowns:
                raise ValueError("Unknown subsystem %r requested for component: %s" % (s, self))

    def __str__(self):
        return "%s@%s" % (self.__class__.__name__, self.name)

    def _get_params(self):
        return {
            'APP_DIR': self.get_option('app_dir'),
            'COMPONENT_DIR': self.get_option('component_dir'),
            'CONFIG_DIR': self.get_option('cfg_dir'),
            'TRACE_DIR': self.get_option('trace_dir'),
        }

    def _get_trace_files(self):
        trace_dir = self.get_option('trace_dir')
        return {
            'install': tr.trace_fn(trace_dir, "install"),
            'start': tr.trace_fn(trace_dir, "start"),
        }

    def known_subsystems(self):
        return set()

    def warm_configs(self):
        pass

    def is_started(self):
        # TODO(harlowja) do a better exhaustive check...
        return tr.TraceReader(self._get_trace_files()['start']).exists()

    def is_installed(self):
        # TODO(harlowja) do a better exhaustive check...
        return tr.TraceReader(self._get_trace_files()['install']).exists()


class PkgInstallComponent(ComponentBase):
    def __init__(self, *args, **kargs):
        ComponentBase.__init__(self, *args, **kargs)
        self.tracewriter = tr.TraceWriter(self._get_trace_files()['install'], break_if_there=False)
        self.packager_factory = packager.PackagerFactory(self.distro, self.distro.get_default_package_manager_cls())

    def _get_download_config(self):
        return (None, None)

    def _clear_pkg_dups(self, pkg_list):
        dup_free_list = []
        names_there = set()
        for pkg in pkg_list:
            if pkg['name'] not in names_there:
                dup_free_list.append(pkg)
                names_there.add(pkg['name'])
        return dup_free_list

    def _get_download_location(self):
        (section, key) = self._get_download_config()
        if not section or not key:
            return (None, None)
        uri = self.cfg.getdefaulted(section, key).strip()
        if not uri:
            raise ValueError(("Could not find uri in config to download "
                               "from at section %s for option %s") % (section, key))
        return (uri, self.get_option('app_dir'))

    def download(self):
        (from_uri, target_dir) = self._get_download_location()
        if not from_uri and not target_dir:
            return []
        else:
            uris = [from_uri]
            utils.log_iterable(uris, logger=LOG,
                    header="Downloading from %s uris" % (len(uris)))
            self.tracewriter.download_happened(target_dir, from_uri)
            dirs_made = down.download(self.distro, from_uri, target_dir)
            self.tracewriter.dirs_made(*dirs_made)
            return uris

    def _get_param_map(self, config_fn):
        mp = ComponentBase._get_params(self)
        if config_fn:
            mp['CONFIG_FN'] = config_fn
        return mp

    @property
    def packages(self):
        pkg_list = self.get_option('packages', [])
        if not pkg_list:
            pkg_list = []
        for name, values in self.subsystems.items():
            if 'packages' in values:
                LOG.debug("Extending package list with packages for subsystem: %r", name)
                pkg_list.extend(values.get('packages'))
        pkg_list = self._clear_pkg_dups(pkg_list)
        return pkg_list

    def install(self):
        LOG.debug('Preparing to install packages for: %r', self.name)
        pkgs = self.packages
        if pkgs:
            pkg_names = [p['name'] for p in pkgs]
            utils.log_iterable(pkg_names, logger=LOG,
                header="Setting up %s distribution packages" % (len(pkg_names)))
            with utils.progress_bar('Installing', len(pkgs)) as p_bar:
                for (i, p) in enumerate(pkgs):
                    self.tracewriter.package_installed(p)
                    self.packager_factory.get_packager_for(p).install(p)
                    p_bar.update(i + 1)
        return self.get_option('trace_dir')

    def pre_install(self):
        pkgs = self.packages
        if pkgs:
            for p in pkgs:
                self.packager_factory.get_packager_for(p).pre_install(p, self._get_param_map(None))

    def post_install(self):
        pkgs = self.packages
        if pkgs:
            for p in self.packages:
                self.packager_factory.get_packager_for(p).post_install(p, self._get_param_map(None))

    def _get_config_files(self):
        return list()

    def _config_adjust(self, contents, config_fn):
        return contents

    def _get_target_config_name(self, config_fn):
        cfg_dir = self.get_option('cfg_dir')
        return sh.joinpths(cfg_dir, config_fn)

    def _get_source_config(self, config_fn):
        return utils.load_template(self.name, config_fn)

    def _get_link_dir(self):
        return sh.joinpths(self.distro.get_command_config('base_link_dir'), self.name)

    def _get_symlinks(self):
        links = dict()
        for fn in self._get_config_files():
            source_fn = self._get_target_config_name(fn)
            links[source_fn] = sh.joinpths(self._get_link_dir(), fn)
        return links

    def _config_param_replace(self, config_fn, contents, parameters):
        return utils.param_replace(contents, parameters)

    def _configure_files(self):
        config_fns = self._get_config_files()
        if config_fns:
            utils.log_iterable(config_fns, logger=LOG,
                header="Configuring %s files" % (len(config_fns)))
            for fn in config_fns:
                tgt_fn = self._get_target_config_name(fn)
                self.tracewriter.dirs_made(*sh.mkdirslist(sh.dirname(tgt_fn)))
                LOG.info("Configuring file %s.", colorizer.quote(fn))
                (source_fn, contents) = self._get_source_config(fn)
                LOG.debug("Replacing parameters in file %r", source_fn)
                contents = self._config_param_replace(fn, contents, self._get_param_map(fn))
                LOG.debug("Applying final adjustments in file %r", source_fn)
                contents = self._config_adjust(contents, fn)
                LOG.info("Writing configuration file %s to %s.", colorizer.quote(source_fn), colorizer.quote(tgt_fn))
                self.tracewriter.cfg_file_written(sh.write_file(tgt_fn, contents))
        return len(config_fns)

    def _configure_symlinks(self):
        links = self._get_symlinks()
        # This sort happens so that we link in the correct order
        # although it might not matter. Either way. We ensure that the right
        # order happens. Ie /etc/blah link runs before /etc/blah/blah
        link_srcs = sorted(links.keys())
        link_srcs.reverse()
        links_made = 0
        for source in link_srcs:
            link = links.get(source)
            try:
                LOG.info("Symlinking %s to %s.", colorizer.quote(link), colorizer.quote(source))
                self.tracewriter.dirs_made(*sh.symlink(source, link))
                self.tracewriter.symlink_made(link)
                links_made += 1
            except OSError as e:
                LOG.warn("Symlinking %s to %s failed: %s", colorizer.quote(link), colorizer.quote(source), e)
        return links_made

    def configure(self):
        return self._configure_files() + self._configure_symlinks()


class PythonInstallComponent(PkgInstallComponent):
    def __init__(self, *args, **kargs):
        PkgInstallComponent.__init__(self, *args, **kargs)
        self.pip_factory = packager.PackagerFactory(self.distro, pip.Packager)
        self.requires_files = [
            sh.joinpths(self.get_option('app_dir'), 'tools', 'pip-requires'),
            sh.joinpths(self.get_option('app_dir'), 'tools', 'test-requires')
        ]

    def _get_download_config(self):
        down_name = self.name.replace("-", "_").lower().strip()
        return ('download_from', down_name)

    def _get_python_directories(self):
        py_dirs = {}
        app_dir = self.get_option('app_dir')
        if sh.isdir(app_dir):
            py_dirs[self.name] = app_dir
        return py_dirs

    @property
    def packages(self):
        pkg_list = super(PythonInstallComponent, self).packages
        if not pkg_list:
            pkg_list = []
        pkg_list.extend(self._get_mapped_packages())
        return pkg_list

    @property
    def pips_to_packages(self):
        pip_pkg_list = self.get_option('pip_to_package', [])
        if not pip_pkg_list:
            pip_pkg_list = []
        return pip_pkg_list

    def _match_pip_requires(self, pip_requirement):
        # Try to find it in anyones pip -> pkg list
        pip2_pkg_mp = {
            self.name: self.pips_to_packages,
        }

        # TODO(harlowja) Is this a bug?? that this is needed?
        def pip_match(in1, in2):
            in1 = in1.replace("-", "_")
            in1 = in1.lower()
            in2 = in2.replace('-', '_')
            in2 = in2.lower()
            return in1 == in2

        for name, component in self.instances.items():
            if component is self or not component.activated:
                continue
            if hasattr(component, 'pips_to_packages'):
                pip2_pkg_mp[name] = component.pips_to_packages

        pip_name = pip_requirement.project_name
        pip_found = False
        pkg_found = None
        for who, pips_2_pkgs in pip2_pkg_mp.items():
            for pip_info in pips_2_pkgs:
                if pip_match(pip_name, pip_info['name']):
                    version = pip_info.get('version')
                    if version is None or version in pip_requirement:
                        # Assume pip installs the right version
                        # for now, TODO(harlowja), make this better
                        pip_found = True
                        pkg_found = pip_info.get('package')
                        LOG.debug("Matched pip->pkg (%s) from component %s", pip_requirement, who)
                        break
            if pip_found:
                break
        if pip_found:
            return (pkg_found, False, True)

        # Ok nobody had it in a pip->pkg mapping
        # but see if they had it in there pip collection
        pip_mp = {
            self.name: list(self.pips),
        }
        for name, component in self.instances.items():
            if not component.activated or component is self:
                continue
            if hasattr(component, 'pips'):
                pip_mp[name] = list(component.pips)

        pip_found = False
        for who, pips in pip_mp.items():
            for pip_info in pips:
                if pip_match(pip_info['name'], pip_name):
                    version = pip_info.get('version')
                    if version is None or version in pip_requirement:
                        # Assume pip installs the right version
                        # for now, TODO(harlowja), make this better
                        pip_found = True
                        LOG.debug("Matched pip (%s) from component %s", pip_requirement, who)
                        break
            if pip_found:
                break
        if pip_found:
            return (None, True, True)
        else:
            return (None, False, False)

    def _get_mapped_packages(self):
        add_on_pkgs = []
        for fn in self.requires_files:
            if sh.isfile(fn):
                LOG.info("Injected & resolving dependencies from %s.", colorizer.quote(fn))
                for line in sh.load_file(fn).splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    requirement = pkg_resources.Requirement.parse(line)
                    (pkg_match, from_pip, already_satisfied) = self._match_pip_requires(requirement)
                    if not pkg_match and not already_satisfied:
                        raise excp.DependencyException(("Pip dependency %r"
                                                        ' (from %r)'
                                                        ' not translatable'
                                                        ' to a known pip package'
                                                        ' or a distribution'
                                                        ' package!') % (requirement, fn))
                    elif already_satisfied:
                        pass
                    else:
                        if not from_pip and pkg_match:
                            add_on_pkgs.append(pkg_match)
        return add_on_pkgs

    @property
    def pips(self):
        pip_list = self.get_option('pips')
        if not pip_list:
            pip_list = []
        for (name, values) in self.subsystems.items():
            if 'pips' in values:
                LOG.debug("Extending pip list with pips for subsystem: %r" % (name))
                pip_list.extend(values.get('pips'))
        pip_list = self._clear_pkg_dups(pip_list)
        return pip_list

    def _install_pips(self):
        pips = self.pips
        if pips:
            pip_names = [p['name'] for p in pips]
            utils.log_iterable(pip_names, logger=LOG,
                header="Setting up %s python packages" % (len(pip_names)))
            with utils.progress_bar('Installing', len(pips)) as p_bar:
                for (i, p) in enumerate(pips):
                    self.tracewriter.pip_installed(p)
                    self.pip_factory.get_packager_for(p).install(p)
                    p_bar.update(i + 1)

    def _clean_pip_requires(self):
        # Fixup these files if they exist (sometimes they have junk in them)
        files = 0
        for fn in self.requires_files:
            if not sh.isfile(fn):
                continue
            new_lines = []
            for line in sh.load_file(fn).splitlines():
                s_line = line.strip()
                if len(s_line) == 0:
                    continue
                elif s_line.startswith("#"):
                    new_lines.append(s_line)
                elif not self._filter_pip_requires_line(s_line):
                    new_lines.append(("# %s" % (s_line)))
                else:
                    new_lines.append(s_line)
            sh.move(fn, "%s.orig" % (fn))
            new_fc = "\n".join(new_lines)
            sh.write_file(fn, "# Cleaned on %s\n\n%s\n" % (date.rcf8222date(), new_fc))
            files += 1
        return files

    def _filter_pip_requires_line(self, line):
        return line

    def pre_install(self):
        PkgInstallComponent.pre_install(self)
        pips = self.pips
        for p in pips:
            self.pip_factory.get_packager_for(p).pre_install(p, self._get_param_map(None))

    def post_install(self):
        PkgInstallComponent.post_install(self)
        pips = self.pips
        for p in pips:
            self.pip_factory.get_packager_for(p).post_install(p, self._get_param_map(None))

    def _install_python_setups(self):
        py_dirs = self._get_python_directories()
        if py_dirs:
            real_dirs = dict()
            for (name, wkdir) in py_dirs.items():
                real_dirs[name] = wkdir
                if not real_dirs[name]:
                    real_dirs[name] = self.get_option('app_dir')
            utils.log_iterable(real_dirs.values(), logger=LOG,
                header="Setting up %s python directories" % (len(real_dirs)))
            setup_cmd = self.distro.get_command('python', 'setup')
            for (name, working_dir) in real_dirs.items():
                self.tracewriter.dirs_made(*sh.mkdirslist(working_dir))
                self.tracewriter.py_installed(name, working_dir)
                root_fn = sh.joinpths(self.get_option('trace_dir'), "%s.python.setup" % (name))
                sh.execute(*setup_cmd,
                           cwd=working_dir,
                           run_as_root=True,
                           stderr_fn='%s.stderr' % (root_fn),
                           stdout_fn='%s.stdout' % (root_fn),
                           trace_writer=self.tracewriter
                           )

    def _python_install(self):
        self._install_pips()
        self._install_python_setups()

    def install(self):
        trace_dir = PkgInstallComponent.install(self)
        self._python_install()
        return trace_dir

    def configure(self):
        am = PkgInstallComponent.configure(self)
        am += self._clean_pip_requires()
        return am


class PkgUninstallComponent(ComponentBase):
    def __init__(self, *args, **kargs):
        ComponentBase.__init__(self, *args, **kargs)
        self.tracereader = tr.TraceReader(self._get_trace_files()['install'])
        self.packager_factory = packager.PackagerFactory(self.distro, self.distro.get_default_package_manager_cls())

    def unconfigure(self):
        self._unconfigure_files()
        self._unconfigure_links()

    def _unconfigure_links(self):
        sym_files = self.tracereader.symlinks_made()
        if sym_files:
            utils.log_iterable(sym_files, logger=LOG,
                header="Removing %s symlink files" % (len(sym_files)))
            for fn in sym_files:
                sh.unlink(fn, run_as_root=True)

    def _unconfigure_files(self):
        cfg_files = self.tracereader.files_configured()
        if cfg_files:
            utils.log_iterable(cfg_files, logger=LOG,
                header="Removing %s configuration files" % (len(cfg_files)))
            for fn in cfg_files:
                sh.unlink(fn, run_as_root=True)

    def uninstall(self):
        self._uninstall_pkgs()
        self._uninstall_touched_files()
        self._uninstall_dirs()
        LOG.debug("Deleting install trace file %r", self.tracereader.filename())
        sh.unlink(self.tracereader.filename())

    def post_uninstall(self):
        pass

    def pre_uninstall(self):
        pass

    def _uninstall_pkgs(self):
        if self.get_option('keep_old', False):
            LOG.info('Keep-old flag set, not removing any packages.')
        else:
            pkgs = self.tracereader.packages_installed()
            if pkgs:
                pkg_names = set([p['name'] for p in pkgs])
                utils.log_iterable(pkg_names, logger=LOG,
                    header="Potentially removing %s packages" % (len(pkg_names)))
                which_removed = set()
                with utils.progress_bar('Uninstalling', len(pkgs), reverse=True) as p_bar:
                    for (i, p) in enumerate(pkgs):
                        if self.packager_factory.get_packager_for(p).remove(p):
                            which_removed.add(p['name'])
                        p_bar.update(i + 1)
                utils.log_iterable(which_removed, logger=LOG,
                    header="Actually removed %s packages" % (len(which_removed)))

    def _uninstall_touched_files(self):
        files_touched = self.tracereader.files_touched()
        if files_touched:
            utils.log_iterable(files_touched, logger=LOG,
                header="Removing %s touched files" % (len(files_touched)))
            for fn in files_touched:
                sh.unlink(fn, run_as_root=True)

    def _uninstall_dirs(self):
        dirs_made = self.tracereader.dirs_made()
        if dirs_made:
            dirs_made = [sh.abspth(d) for d in dirs_made]
            if self.get_option('keep_old', False):
                download_places = [path_location[0] for path_location in self.tracereader.download_locations()]
                if download_places:
                    utils.log_iterable(download_places, logger=LOG,
                        header="Keeping %s download directories (and there children directories)" % (len(download_places)))
                    for download_place in download_places:
                        dirs_made = sh.remove_parents(download_place, dirs_made)
            if dirs_made:
                utils.log_iterable(dirs_made, logger=LOG,
                    header="Removing %s created directories" % (len(dirs_made)))
                for dir_name in dirs_made:
                    if sh.isdir(dir_name):
                        sh.deldir(dir_name, run_as_root=True)
                    else:
                        LOG.warn("No directory found at %s - skipping", colorizer.quote(dir_name, quote_color='red'))


class PythonUninstallComponent(PkgUninstallComponent):
    def __init__(self, *args, **kargs):
        PkgUninstallComponent.__init__(self, *args, **kargs)
        self.pip_factory = packager.PackagerFactory(self.distro, pip.Packager)

    def uninstall(self):
        self._uninstall_python()
        self._uninstall_pips()
        PkgUninstallComponent.uninstall(self)

    def _uninstall_pips(self):
        if self.get_option('keep_old', False):
            LOG.info('Keep-old flag set, not removing any python packages.')
        else:
            pips = self.tracereader.pips_installed()
            if pips:
                pip_names = set([p['name'] for p in pips])
                utils.log_iterable(pip_names, logger=LOG,
                    header="Uninstalling %s python packages" % (len(pip_names)))
                with utils.progress_bar('Uninstalling', len(pips), reverse=True) as p_bar:
                    for (i, p) in enumerate(pips):
                        try:
                            self.pip_factory.get_packager_for(p).remove(p)
                        except excp.ProcessExecutionError as e:
                            # NOTE(harlowja): pip seems to die if a pkg isn't there even in quiet mode
                            combined = (str(e.stderr) + str(e.stdout))
                            if not re.search(r"not\s+installed", combined, re.I):
                                raise
                        p_bar.update(i + 1)

    def _uninstall_python(self):
        py_listing = self.tracereader.py_listing()
        if py_listing:
            py_listing_dirs = set()
            for (name, where) in py_listing:
                py_listing_dirs.add(where)
            utils.log_iterable(py_listing_dirs, logger=LOG,
                header="Uninstalling %s python setups" % (len(py_listing_dirs)))
            unsetup_cmd = self.distro.get_command('python', 'unsetup')
            for where in py_listing_dirs:
                if sh.isdir(where):
                    sh.execute(*unsetup_cmd, cwd=where, run_as_root=True)
                else:
                    LOG.warn("No python directory found at %s - skipping", colorizer.quote(where, quote_color='red'))


class ProgramRuntime(ComponentBase):
    def __init__(self, *args, **kargs):
        ComponentBase.__init__(self, *args, **kargs)
        self.tracewriter = tr.TraceWriter(self._get_trace_files()['start'], break_if_there=True)
        self.tracereader = tr.TraceReader(self._get_trace_files()['start'])

    def _get_apps_to_start(self):
        return list()

    def _get_app_options(self, app_name):
        return list()

    def _get_param_map(self, app_name):
        mp = ComponentBase._get_params(self)
        if app_name:
            mp['APP_NAME'] = app_name
        return mp

    def _fetch_run_type(self):
        return self.cfg.getdefaulted("DEFAULT", "run_type", 'anvil.runners.fork:ForkRunner')

    def start(self):
        # Anything to start?
        am_started = 0
        apps_to_start = self._get_apps_to_start()
        if not apps_to_start:
            return am_started
        # Select how we are going to start it
        run_type = self._fetch_run_type()
        starter = importer.import_entry_point(run_type)(self)
        for app_info in apps_to_start:
            app_name = app_info["name"]
            app_pth = app_info.get("path", app_name)
            app_dir = app_info.get("app_dir", self.get_option('app_dir'))
            # Adjust the program options now that we have real locations
            program_opts = utils.param_replace_list(self._get_app_options(app_name), self._get_param_map(app_name))
            # Start it with the given settings
            LOG.debug("Starting %r using %r", app_name, run_type)
            details_fn = starter.start(app_name, app_pth=app_pth, app_dir=app_dir, opts=program_opts)
            LOG.info("Started %s details are in %s", colorizer.quote(app_name), colorizer.quote(details_fn))
            # This trace is used to locate details about what to stop
            self.tracewriter.app_started(app_name, details_fn, run_type)
            if app_info.get('sleep_time'):
                LOG.info("%s requested a %s second sleep time, please wait...", colorizer.quote(app_name), app_info.get('sleep_time'))
                sh.sleep(app_info.get('sleep_time'))
            am_started += 1
        return am_started

    def _locate_investigators(self, apps_started):
        investigators = dict()
        to_investigate = list()
        for (app_name, trace_fn, how) in apps_started:
            inv_cls = None
            try:
                inv_cls = importer.import_entry_point(how)
            except RuntimeError as e:
                LOG.warn("Could not load class %s which should be used to investigate %s: %s",
                            colorizer.quote(how), colorizer.quote(app_name), e)
                continue
            investigator = None
            if inv_cls in investigators:
                investigator = investigators[inv_cls]
            else:
                investigator = inv_cls(self)
                investigators[inv_cls] = investigator
            to_investigate.append((app_name, investigator))
        return to_investigate

    def stop(self):
        # Anything to stop??
        killed_am = 0
        apps_started = self.tracereader.apps_started()
        if not apps_started:
            return killed_am
        to_kill = self._locate_investigators(apps_started)
        for (app_name, handler) in to_kill:
            handler.stop(app_name)
            handler.unconfigure()
            killed_am += 1
        if len(apps_started) == killed_am:
            sh.unlink(self.tracereader.filename())
        return killed_am

    def _multi_status(self):
        try:
            apps_started = self.tracereader.apps_started()
        except excp.NoTraceException:
            return None
        if not apps_started:
            return None
        else:
            to_check = self._locate_investigators(apps_started)
            results = dict()
            for (name, handler) in to_check:
                try:
                    results[name] = handler.status(name)
                except AttributeError:
                    pass  # Not all handlers can implement this..
            return results

    def _status(self):
        return constants.STATUS_UNKNOWN

    def status(self):
        stat = self._multi_status()
        if not stat:
            stat = self._status()
        if not stat or stat == constants.STATUS_UNKNOWN:
            if self.is_installed():
                stat = constants.STATUS_INSTALLED
            elif self.is_started():
                stat = constants.STATUS_STARTED
            else:
                stat = constants.STATUS_UNKNOWN
        return stat

    def restart(self):
        return 0


class PythonRuntime(ProgramRuntime):
    def __init__(self, *args, **kargs):
        ProgramRuntime.__init__(self, *args, **kargs)


class EmptyRuntime(ProgramRuntime):
    def __init__(self, *args, **kargs):
        ProgramRuntime.__init__(self, *args, **kargs)
