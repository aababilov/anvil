# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#    Copyright (C) 2012 New Dream Network, LLC (DreamHost) All Rights Reserved.
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


from anvil import component


class Installer(component.PythonInstallComponent):
    @property
    def packages(self):
        pkg_list = super(Installer, self).packages
        if not pkg_list:
            pkg_list = []
        pips_to_packages = self.pips_to_packages
        for pip_to_package in pips_to_packages:
            if 'package' in pip_to_package:
                pkg_list.append(pip_to_package['package'])
        return pkg_list

    def _get_python_directories(self):
        return {}

    def _get_download_config(self):
        return (None, None)


class Uninstaller(component.PythonUninstallComponent):
    pass
