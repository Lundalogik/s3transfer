#!/usr/bin/env python
from contextlib import contextmanager
import os.path
import shutil
import sys
import os
from subprocess import call, check_call, check_output, CalledProcessError
from os.path import abspath, dirname
import glob
from wheel.install import WheelFile
import getpass
import logging
import click


DEFAULT_PYPI_INDEX = 'https://pypi.lundalogik.com:3443/lime/develop'

logger = logging.getLogger(__name__)
ROOT = os.path.abspath(os.path.dirname(__file__))


@click.group(context_settings={'help_option_names': ['--help', '-h']})
@click.option(
    '--loglevel', default='INFO',
    type=click.Choice(['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG']))
def cli(loglevel):
    _setup_logger(loglevel)


@cli.command(help='Build package')
def build():
    rm_rf('build')
    rm_rf('dist')
    check_call(['python', 'setup.py', '-q', 'bdist_wheel'])


@cli.command(help="Build wheel and upload to internal pypi server")
@click.option('--force', '-f', default=False, is_flag=True, help="Force")
@click.option('--username', '-u', help='Username for uploading to internal '
              'pypi server')
@click.option('--password', '-p', help='Password')
@click.option('--index', '-i', default=DEFAULT_PYPI_INDEX,
              help='Pypi index to use.')
@click.pass_context
def upload(ctx, username=None, password=None, index=DEFAULT_PYPI_INDEX,
           force=False):
    ctx.invoke(build)

    def package_exists(path):
        parsed_filename = WheelFile(path).parsed_filename
        package, version = parsed_filename.group(2), parsed_filename.group(4)
        try:
            p = check_output(
                'devpi list {}=={}'.format(package, version).split())
            exists = True if p else False
        except CalledProcessError as e:
            if '404 Not Found: no project' in e.stdout.decode('utf-8'):
                exists = False
            else:
                raise

        if exists:
            print('Package {}={} already exists.'.format(package, version))
        return exists

    def get_wheel_path():
        dist_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), 'dist'))
        return next(iter(glob.glob('{}/*.whl'.format(dist_dir))))

    check_call(['devpi', 'use', index])

    wheel_path = get_wheel_path()
    if not package_exists(wheel_path) or force:
        if username:
            if not password:
                password = getpass.getpass()
            check_call(['devpi', 'login', username, '--password', password])

        check_call(['devpi', 'upload', wheel_path])


def rm(path):
    if os.path.isfile(path):
        os.remove(path)


def rm_rf(path):
    if os.path.isdir(path):
        shutil.rmtree(path)


@contextmanager
def cd(path):
    cwd = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(cwd)


def _setup_logger(level):
    global_log = logging.getLogger()
    global_log.setLevel(getattr(logging, level))
    global_log.addHandler(logging.StreamHandler(sys.stdout))


if __name__ == '__main__':
    with cd(ROOT):
        sys.exit(cli())
