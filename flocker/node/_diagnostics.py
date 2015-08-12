# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
A script to export Flocker log files and system information.
"""

from gzip import open as gzip_open
import os
from platform import dist as platform_dist
import re
from shutil import copyfileobj, make_archive, rmtree
from socket import gethostname
from subprocess import check_call, check_output
from time import time

from flocker import __version__


def gzip_file(source_path, archive_path):
    """
    Create a gzip compressed archive of ``source_path`` at ``archive_path``.
    An empty archive file will be created if the source file does not exist.
    """
    with gzip_open(archive_path, 'wb') as archive:
        if os.path.isfile(source_path):
            with open(source_path, 'rb') as source:
                copyfileobj(source, archive)


class FlockerDebugArchive(object):
    """
    Create a tar archive containing:
    * logs from all installed Flocker services,
    * some or all of the syslog depending on the logging system,
    * Docker version and configuration information, and
    * a list of all the services installed on the system and their status.
    """
    def __init__(self, service_manager, log_exporter):
        """
        :param service_manager: An API for listing installed services.
        :param log_exporter: An API for exporting logs for services.
        """
        self._service_manager = service_manager
        self._log_exporter = log_exporter

        self._suffix = "{}_{}".format(
            gethostname(),
            time()
        )
        self._archive_name = "clusterhq_flocker_logs_{}".format(
            self._suffix
        )
        self._archive_path = os.path.abspath(self._archive_name)

    def _logfile_path(self, name):
        """
        Generate a path to a file inside the archive directory.

        :param str name: A unique label for the file.
        :returns: An absolute path string for a file inside the archive
            directory.
        """
        return os.path.join(
            self._archive_name,
            name,
        )

    def _open_logfile(self, name):
        """
        :param str name: A unique label for the file.
        :return: An open ``file`` object with a name generated by
            `_logfile_path`.
        """
        return open(self._logfile_path(name), 'w')

    def create(self):
        """
        Create the archive by first creating a uniquely named directory in the
        current working directory, adding the log files and debug information,
        creating a ``tar`` archive from the directory and finally removing the
        directory.
        """
        os.makedirs(self._archive_path)
        try:
            # Export Flocker version
            with self._open_logfile('flocker-version') as output:
                output.write(__version__.encode('utf-8') + b'\n')

            # Export Flocker logs.
            services = self._service_manager.flocker_services()
            for service_name, service_status in services:
                self._log_exporter.export_flocker(
                    service_name=service_name,
                    target_path=self._logfile_path(service_name)
                )
            # Export syslog.
            self._log_exporter.export_all(self._logfile_path('syslog'))

            # Export the status of all services.
            with self._open_logfile('service-status') as output:
                services = self._service_manager.all_services()
                for service_name, service_status in services:
                    output.write(service_name + " " + service_status + "\n")

            # Export Docker version and configuration
            check_call(
                ['docker', 'version'],
                stdout=self._open_logfile('docker-version')
            )
            check_call(
                ['docker', 'info'],
                stdout=self._open_logfile('docker-info')
            )

            # Export Kernel version
            self._open_logfile('uname').write(' '.join(os.uname()))

            # Export Distribution version
            self._open_logfile('os-release').write(
                open('/etc/os-release').read()
            )

            # Create a single archive file
            archive_path = make_archive(
                base_name=self._archive_name,
                format='tar',
                root_dir=os.path.dirname(self._archive_path),
                base_dir=os.path.basename(self._archive_path),
            )
        finally:
            # Attempt to remove the source directory.
            rmtree(self._archive_path)
        return archive_path


class SystemdServiceManager(object):
    """
    List services managed by Systemd.
    """
    def all_services(self):
        """
        Iterate the name and status of all services known to SystemD.
        """
        output = check_output(['systemctl', 'list-unit-files', '--no-legend'])
        for line in output.splitlines():
            line = line.rstrip()
            service_name, service_status = line.split(None, 1)
            yield service_name, service_status

    def flocker_services(self):
        """
        Iterate the name and status of the Flocker services known to SystemD.
        """
        service_pattern = r'^(?P<service_name>flocker-.+)\.service'
        for service_name, service_status in self.all_services():
            match = re.match(service_pattern, service_name)
            if match:
                service_name = match.group('service_name')
                if service_status == 'enabled':
                    yield service_name, service_status


class UpstartServiceManager(object):
    """
    List services managed by Upstart.
    """
    def all_services(self):
        """
        Iterate the name and status of all services known to Upstart.
        """
        for line in check_output(['initctl', 'list']).splitlines():
            service_name, service_status = line.split(None, 1)
            yield service_name, service_status

    def flocker_services(self):
        """
        Iterate the name and status of the Flocker services known to Upstart.
        """
        for service_name, service_status in self.all_services():
            if service_name.startswith('flocker-'):
                yield service_name, service_status


class JournaldLogExporter(object):
    """
    Export logs managed by JournalD.
    """
    def export_flocker(self, service_name, target_path):
        """
        Export logs for ``service_name`` to ``target_path`` compressed using
        ``gzip``.
        """
        # Centos-7 doesn't have separate startup logs.
        open(target_path + '_startup.gz', 'w').close()
        check_call(
            'journalctl --all --output cat --unit {}.service '
            '| gzip'.format(service_name),
            stdout=open(target_path + '_eliot.gz', 'w'),
            shell=True
        )

    def export_all(self, target_path):
        """
        Export all system logs to ``target_path`` compressed using ``gzip``.
        """
        check_call(
            'journalctl --all --boot | gzip',
            stdout=open(target_path + '.gz', 'w'),
            shell=True
        )


class UpstartLogExporter(object):
    """
    Export logs for services managed by Upstart and written by RSyslog.
    """
    def export_flocker(self, service_name, target_path):
        """
        Export logs for ``service_name`` to ``target_path`` compressed using
        ``gzip``.
        """
        files = [
            ("/var/log/upstart/{}.log".format(service_name),
             target_path + '_startup.gz'),
            ("/var/log/flocker/{}.log".format(service_name),
             target_path + '_eliot.gz'),
        ]
        for source_path, archive_path in files:
            gzip_file(source_path, archive_path)

    def export_all(self, target_path):
        """
        Export all system logs to ``target_path`` compressed using ``gzip``.
        """
        gzip_file('/var/log/syslog', target_path + '.gz')


class Distribution(object):
    """
    A record of the service manager and log exported to be used on each
    supported Linux distribution.
    """
    def __init__(self, name, version, service_manager, log_exporter):
        """
        :param str name: The name of the operating system.
        :param str version: The version of the operating system.
        :param service_manager: The service manager API to use for this
            operating system.
        :param log_exporter: The log exporter API to use for this operating
            system.
        """
        self.name = name
        self.version = version
        self.service_manager = service_manager
        self.log_exporter = log_exporter


DISTRIBUTIONS = (
    Distribution(
        name='centos',
        version='7',
        service_manager=SystemdServiceManager,
        log_exporter=JournaldLogExporter,
    ),
    Distribution(
        name='fedora',
        version='22',
        service_manager=SystemdServiceManager,
        log_exporter=JournaldLogExporter,
    ),
    Distribution(
        name='ubuntu',
        version='14.04',
        service_manager=UpstartServiceManager,
        log_exporter=UpstartLogExporter,
    )
)


_DISTRIBUTION_BY_LABEL = dict(
    ('{}-{}'.format(p.name, p.version), p)
    for p in DISTRIBUTIONS
)


class UnsupportedDistribution(Exception):
    """
    The distribution is not supported.
    """
    def __init__(self, distribution):
        """
        :param str distribution: The unsupported distribution.
        """
        self.distribution = distribution


def current_distribution():
    """
    :returns: A ``Platform`` for the operating system where this script.
    :raises: ``UnsupportedPlatform`` if the current platform is unsupported.
    """
    name, version, nickname = platform_dist()
    current_distribution_label = name.lower() + '-' + version
    for distribution_label, distribution in _DISTRIBUTION_BY_LABEL.items():
        if current_distribution_label.startswith(distribution_label):
            return distribution
    else:
        raise UnsupportedDistribution(current_distribution_label)
