#!/usr/bin/python

import logging
import os
import os.path
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import urllib

import dcos_login
import github_update

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format="%(message)s")

class CITester(object):

    def __init__(self, dcos_url, github_label):
        self.__CLI_URL_TEMPLATE = 'https://downloads.dcos.io/binaries/cli/{}/x86-64/latest/{}'
        self.__dcos_url = dcos_url
        self.__sandbox_path = ''
        self.__github_updater = github_update.GithubStatusUpdater('test:{}'.format(github_label))


    def __configure_cli_sandbox(self):
        self.__sandbox_path = tempfile.mkdtemp(prefix='ci-test-')
        custom_env = {}
        # must be custom for CLI to behave properly:
        custom_env['HOME'] = self.__sandbox_path
        # prepend HOME (where CLI binary is downloaded) to PATH:
        custom_env['PATH'] = '{}:{}'.format(self.__sandbox_path, os.environ['PATH'])
        # must be explicitly provided for CLI to behave properly:
        custom_env['DCOS_CONFIG'] = os.path.join(self.__sandbox_path, 'cli-config')
        # optional:
        #custom_env['DCOS_DEBUG'] = custom_env.get('DCOS_DEBUG', 'true')
        #custom_env['DCOS_LOG_LEVEL'] = custom_env.get('DCOS_LOG_LEVEL', 'debug')
        logger.info('Created CLI sandbox: {}, Custom envvars: {}.'.format(self.__sandbox_path, custom_env))
        for k, v in custom_env.items():
            os.environ[k] = v


    def __download_cli_to_sandbox(self):
        cli_filename = 'dcos'
        if sys.platform == 'win32':
            cli_platform = 'windows'
            cli_filename = 'dcos.exe'
        elif sys.platform == 'linux2':
            cli_platform = 'linux'
        elif sys.platform == 'darwin':
            cli_platform = 'darwin'
        else:
            raise Exception('Unsupported platform: {}'.format(sys.platform))
        cli_url = self.__CLI_URL_TEMPLATE.format(cli_platform, cli_filename)
        cli_filepath = os.path.join(self.__sandbox_path, cli_filename)
        local_path = os.environ.get('DCOS_CLI_PATH', '')
        if local_path:
            logger.info('Copying {} to {}'.format(local_path, cli_filepath))
            shutil.copyfile(local_path, cli_filepath)
        else:
            logger.info('Downloading {} to {}'.format(cli_url, cli_filepath))
            urllib.URLopener().retrieve(cli_url, cli_filepath)
        os.chmod(cli_filepath, 0755)
        return cli_filepath


    def __configure_cli(self, dcos_url):
        cmds = [
            'which dcos',
            'dcos config set core.dcos_url "{}"'.format(dcos_url),
            'dcos config set core.reporting True',
            'dcos config set core.ssl_verify false',
            'dcos config set core.timeout 5',
            'dcos config show']
        for cmd in cmds:
            subprocess.check_call(cmd.split(' '))


    def setup_cli(self):
        try:
            self.__github_updater.update('pending', 'Setting up CLI')
            self.__configure_cli_sandbox()  # adds to os.environ
            cli_filepath = self.__download_cli_to_sandbox()
            self.__configure_cli(self.__dcos_url)
            dcos_login.DCOSLogin(self.__dcos_url).login()
        except:
            self.__github_updater.update('error', 'CLI Setup failed')
            raise


    def run_shakedown(self, test_dirs, requirements_file = ''):
        # keep virtualenv in a consistent/reusable location:
        if 'WORKSPACE' in os.environ:
            virtualenv_path = os.path.join(os.environ['WORKSPACE'], 'env')
        else:
            virtualenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'env')
        logger.info('Configuring virtualenv in {}'.format(virtualenv_path))
        # to ensure the 'source' call works, just create a shell script and execute it directly:
        script_path = os.path.join(self.__sandbox_path, 'run_shakedown.sh')
        script_file = open(script_path, 'w')
        if requirements_file:
            requirements_cmd = 'pip install -r {}'.format(requirements_file)
        else:
            requirements_cmd = ''
        # TODO(nick): remove this inlined script with external templating
        #             (or find a way of entering the virtualenv that doesn't involve a shell script)
        script_file.write('''
#!/bin/bash
set -e
echo "VIRTUALENV CREATE/UPDATE: {venv_path}"
virtualenv -p $(which python3) --always-copy {venv_path}
echo "VIRTUALENV ACTIVATE: {venv_path}"
source {venv_path}/bin/activate
echo "REQUIREMENTS INSTALL: {reqs_file}"
{reqs_cmd}
echo "SHAKEDOWN RUN: {test_dirs}"
shakedown --dcos-url "{dcos_url}" --ssh-key-file "" --stdout=all --stdout-inline {test_dirs}
'''.format(venv_path=virtualenv_path,
           reqs_file=requirements_file,
           reqs_cmd=requirements_cmd,
           dcos_url=self.__dcos_url,
           test_dirs=test_dirs))
        script_file.flush()
        script_file.close()
        try:
            self.__github_updater.update('pending', 'Running shakedown tests')
            subprocess.check_call(['bash', script_path])
        except:
            self.__github_updater.update('failure', 'Shakedown tests failed')
            raise

    def run_dcostests(self, test_dirs, dcos_tests_dir, pytest_types='sanity'):
        os.environ['DOCKER_CLI'] = 'false'
        if 'WORKSPACE' in os.environ:
            # produce test report for consumption by Jenkins:
            jenkins_args = '--junitxml=report.xml '
        else:
            jenkins_args = ''
        # to ensure the 'source' call works, just create a shell script and execute it directly:
        script_path = os.path.join(self.__sandbox_path, 'run_dcos_tests.sh')
        script_file = open(script_path, 'w')
        # TODO(nick): remove this inlined script with external templating
        #             (or find a way of entering the virtualenv that doesn't involve a shell script)
        script_file.write('''
#!/bin/bash
set -e
cd {dcos_tests_dir}
echo "{dcos_url}" > docker-context/dcos-url.txt
echo "PYTHON SETUP"
source utils/python_setup
echo "PYTEST RUN $(pwd)"
SSH_KEY_FILE="" PYTHONPATH=$(pwd) py.test {jenkins_args}-vv -s -m "{pytest_types}" {test_dirs}
'''.format(dcos_tests_dir=dcos_tests_dir,
           dcos_url=self.__dcos_url,
           jenkins_args=jenkins_args,
           pytest_types=pytest_types,
           test_dirs=test_dirs))
        script_file.flush()
        script_file.close()
        try:
            self.__github_updater.update('pending', 'Running dcos-tests')
            subprocess.check_call(['bash', script_path])
        except:
            self.__github_updater.update('failure', 'dcos-tests failed')
            raise

    def delete_sandbox(self):
        if not self.__sandbox_path:
            return  # no-op
        logger.info('Deleting CLI sandbox: {}'.format(self.__sandbox_path))
        shutil.rmtree(self.__sandbox_path)


def print_help(argv):
    logger.info('Syntax: {} <"shakedown"|"dcos-tests"> <dcos-url> <path/to/tests/> [/path/to/custom-requirements.txt | /path/to/dcos-tests [test-types]]'.format(argv[0]))
    logger.info('  Example (shakedown w/ requirements): $ {} shakedown http://your-cluster.com /path/to/your/tests/ /path/to/custom/requirements.txt')
    logger.info('  Example (shakedown w/o requirements): $ {} shakedown http://your-cluster.com /path/to/your/tests/')
    logger.info('  Example (dcos-tests, deprecated): $ {} dcos-tests http://your-cluster.com /path/to/your/tests/ /path/to/dcos-tests/ "sanity or recovery"')


def main(argv):
    if len(argv) < 4:
        print_help(argv)
        return 1
    test_type = argv[1]
    dcos_url = argv[2]
    test_dirs = argv[3]

    tester = CITester(dcos_url, os.environ.get('TEST_GITHUB_LABEL', test_type))
    try:
        if test_type == 'shakedown':
            tester.setup_cli()
            if len(argv) >= 5:
                test_requirements = argv[4]
                tester.run_shakedown(test_dirs, test_requirements)
            else:
                tester.run_shakedown(test_dirs)
        elif test_type == 'dcos-tests':
            tester.setup_cli()
            dcos_tests_dir = argv[4]
            if len(argv) >= 6:
                pytest_types = argv[5]
                tester.run_dcostests(test_dirs, dcos_tests_dir, pytest_types)
            else:
                tester.run_dcostests(test_dirs, dcos_tests_dir)
        else:
            raise Exception('Unsupported test type: {}'.format(test_type))

        tester.delete_sandbox()
    except:
        tester.delete_sandbox()
        raise
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
