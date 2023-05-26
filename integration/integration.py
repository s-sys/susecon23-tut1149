#!/usr/bin/env python3

import datetime
import logging
import os
import re
import threading
import multiprocessing

import dateutil.parser
import psycopg2
import psycopg2.extras

from flask import Flask, request, jsonify
from jira_patch import JIRA, CustomFieldType, CustomFieldSearcherKey
from susemanager import SuseManager


# SUSE Manager connection settings
SUSEMANAGER_URL = os.getenv('SUSEMANAGER_URL', 'http://susemanager')
SUSEMANAGER_USERNAME = os.getenv('SUSEMANAGER_USERNAME', 'susemanager')
SUSEMANAGER_PASSWORD = os.getenv('SUSEMANAGER_PASSWORD', 'susemanager')
SUSEMANAGER_UNINSTALL_REVERTED = True

# PostgreSQL connection settings
POSTGRES_HOST = os.getenv('POSTGRES_HOST', 'postgres')
POSTGRES_PORT = int(os.getenv('POSTGRES_PORT', '5432'))
POSTGRES_USERNAME = os.getenv('POSTGRES_USERNAME', 'postgres')
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'postgres')
POSTGRES_DB = os.getenv('POSTGRES_DB', 'postgres')
POSTGRES_AUTH = {
    'host': POSTGRES_HOST,
    'port': POSTGRES_PORT,
    'user': POSTGRES_USERNAME,
    'password': POSTGRES_PASSWORD,
    'dbname': POSTGRES_DB,
}

# Jira connection settings
JIRA_HOST = os.getenv('JIRA_HOST', 'https://jira.atlassian.com')
JIRA_USERNAME = os.getenv('JIRA_USERNAME', 'jira')
JIRA_PASSWORD = os.getenv('JIRA_PASSWORD', 'jira')
JIRA_DEVICES_FIELD = 'Devices'
JIRA_PACKAGE_FIELD = 'Package'

# PostgreSQL queries
SELECT_DEVICES_QUERY = (
    'SELECT id, name '
    'FROM devices'
)

INSERT_DEVICES_QUERY = (
    'INSERT INTO devices '
    '(id, name) '
    'VALUES (%s, %s) '
    'ON CONFLICT (name) '
    'DO UPDATE SET id = EXCLUDED.id, last_seen = NOW()'
)

SELECT_PACKAGES_QUERY = (
    'SELECT id, name, version '
    'FROM packages'
)

INSERT_PACKAGES_QUERY = (
    'INSERT INTO packages '
    '(id, name, version) '
    'VALUES (%s, %s, %s) '
    'ON CONFLICT (name, version) '
    'DO UPDATE SET id = EXCLUDED.id'
)

SELECT_PACKAGE_REQUESTS_QUERY = (
    'SELECT device_name, package_name, package_version, reverted '
    'FROM package_requests '
    'WHERE after <= NOW() '
    'ORDER BY after DESC'
)

SELECT_PACKAGE_REQUESTS_DEVICES_QUERY = (
    'SELECT device_name '
    'FROM package_requests '
    'WHERE itsm_id = %s'
)

INSERT_PACKAGE_REQUESTS_QUERY = (
    'INSERT INTO package_requests '
    '(itsm_id, device_name, package_name, package_version, after) '
    'VALUES (%s, %s, %s, %s, %s) '
    'ON CONFLICT (itsm_id, device_name, package_name, package_version) '
    'DO UPDATE SET after = EXCLUDED.after'
)

UPDATE_PACKAGE_REQUESTS_QUERY = (
    'UPDATE package_requests '
    'SET reverted = TRUE '
    'WHERE itsm_id = %s'
)

INSERT_REBOOT_REQUESTS_QUERY = (
    'INSERT INTO reboot_requests '
    '(itsm_id, device_name, action_id) '
    'VALUES (%s, %s, %s)'
)

# Regular expressions
RE_SPLIT_VERSION = re.compile(r'[\.\-\+\~\:]+')

# Multiprocess locks
SYNC_DEVICES_LOCK = multiprocessing.Lock()
SYNC_DATA_LOCK = multiprocessing.Lock()

# Logging settings
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('Integration')

# Flask settings
app = Flask('Integration')


@app.route('/package/install', methods=['POST'])
def package_install():
    # Get request body
    body = request.get_json(force=True, silent=True)
    if body is None:
        return jsonify({'success': False, 'error': 'Expected a valid JSON request body.'}), 400

    try:
        # Get request values
        itsm_id = body.get('itsm_id')
        device_names = body.get('device_names', body.get('device_name', []))
        device_names = list(set([device_names] if isinstance(device_names, str) else device_names))
        package_name = body.get('package_name')
        package_version = body.get('package_version')
        after = body.get('after')
        after = isoparse(after) if after else datetime.datetime.now()

         # Validate request values
        if not itsm_id:
            return jsonify({'success': False, 'error': 'Expected ITSM ID in field \'itsm_id\'.'}), 400
        if not device_names:
            return jsonify({'success': False, 'error': 'Expected list of devices in field \'device_names\'.'}), 400
        if not package_name:
            return jsonify({'success': False, 'error': 'Expected package name in field \'package_name\'.'}), 400
        if not package_version:
            return jsonify({'success': False, 'error': 'Expected package version in field \'package_version\'.'}), 400
        if not after:
            return jsonify({'success': False, 'error': 'Expected ISO-formatted datetime in field \'after\'.'}), 400
        if package_version.lower() in ('remove', 'latest'):
            package_version = package_version.lower()
        package_version = package_version if package_version != 'remove' else None
    except:
        return jsonify({'success': False, 'error': 'Invalid parameters.'}), 400

    log.info('Install:%s: Received request to install package %s version %s on %s devices.',
             itsm_id, package_name, package_version, len(device_names))

    # Transition issue status to waiting on Jira
    log.info('Install:%s: Transitioning Jira issue status to waiting.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Wait')
    except:
        log.error('Install:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Insert data into the database
    log.info('Install:%s: Inserting package management request into the database.', itsm_id)
    try:
        with psycopg2.connect(**POSTGRES_AUTH) as connection:
            for device_name in device_names:
                try:
                    with connection.cursor() as cursor:
                        values = (itsm_id, device_name, package_name, package_version, after)
                        cursor.execute(INSERT_PACKAGE_REQUESTS_QUERY, values)
                except:
                    log.error('Install:%s: Failed to insert package management request for %s into the database.',
                              itsm_id, device_name, exc_info=True)
    except:
        log.error('Install:%s: Failed to communicate with the database.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to communicate with the database.'}), 500

    # Start task to sync devices
    sync_devices()

    # Transition issue status to completed on Jira
    log.info('Install:%s: Transitioning Jira issue status to completed.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Complete')
    except:
        log.error('Install:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Send success response
    log.info('Install:%s: Finished.', itsm_id)
    return jsonify({'success': True})


@app.route('/package/remove', methods=['POST'])
def package_remove():
    # Get request body
    body = request.get_json(force=True, silent=True)
    if body is None:
        return jsonify({'success': False, 'error': 'Expected a valid JSON request body.'}), 400

    # Get request values
    try:
        itsm_id = body.get('itsm_id')
        device_names = body.get('device_names', body.get('device_name', []))
        device_names = list(set([device_names] if isinstance(device_names, str) else device_names))
        package_name = body.get('package_name')
        after = body.get('after')
        after = isoparse(after) if after else datetime.datetime.now()

        # Validate request values
        if not itsm_id:
            return jsonify({'success': False, 'error': 'Expected ITSM ID in field \'itsm_id\'.'}), 400
        if not device_names:
            return jsonify({'success': False, 'error': 'Expected list of devices in field \'device_names\'.'}), 400
        if not package_name:
            return jsonify({'success': False, 'error': 'Expected package name in field \'package_name\'.'}), 400
        if not after:
            return jsonify({'success': False, 'error': 'Expected ISO-formatted datetime in field \'after\'.'}), 400
    except:
        return jsonify({'success': False, 'error': 'Invalid parameters.'}), 400

    log.info('Remove:%s: Received request to remove package %s on %s devices.',
             itsm_id, package_name, len(device_names))

    # Transition issue status to waiting on Jira
    log.info('Remove:%s: Transitioning Jira issue status to waiting.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Wait')
    except:
        log.error('Remove:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Insert data into the database
    log.info('Remove:%s: Inserting package management request into the database.', itsm_id)
    try:
        with psycopg2.connect(**POSTGRES_AUTH) as connection:
            for device_name in device_names:
                try:
                    with connection.cursor() as cursor:
                        values = (itsm_id, device_name, package_name, None, after)
                        cursor.execute(INSERT_PACKAGE_REQUESTS_QUERY, values)
                except:
                    log.error('Remove:%s: Failed to insert package management request for %s into the database.',
                              itsm_id, device_name, exc_info=True)
    except:
        log.error('Remove:%s: Failed to communicate with the database.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to communicate with the database.'}), 500

    # Start task to sync devices
    sync_devices()

    # Transition issue status to completed on Jira
    log.info('Remove:%s: Transitioning Jira issue status to completed.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Complete')
    except:
        log.error('Remove:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Send success response
    log.info('Remove:%s: Finished.', itsm_id)
    return jsonify({'success': True})


@app.route('/package/revert', methods=['POST'])
def package_revert():
    # Get request body
    body = request.get_json(force=True, silent=True)
    if body is None:
        return jsonify({'success': False, 'error': 'Expected a valid JSON request body.'}), 400

    try:
        # Get request values
        itsm_id = body.get('itsm_id')

        # Validate request values
        if not itsm_id:
            return jsonify({'success': False, 'error': 'Expected ITSM ID in field \'itsm_id\'.'}), 400
    except:
        return jsonify({'success': False, 'error': 'Invalid parameters.'}), 400

    log.info('Revert:%s: Received request to revert issue %s.', itsm_id, itsm_id)

    # Transition issue status to waiting on Jira
    log.info('Revert:%s: Transitioning Jira issue status to waiting.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Wait')
    except:
        log.error('Revert:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Update data in the database
    log.info('Revert:%s: Inserting package management request into the database.', itsm_id)
    try:
        with psycopg2.connect(**POSTGRES_AUTH) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(UPDATE_PACKAGE_REQUESTS_QUERY, (itsm_id,))
                connection.commit()
                with connection.cursor() as cursor:
                    cursor.execute(SELECT_PACKAGE_REQUESTS_DEVICES_QUERY, (itsm_id,))
                    device_names = list(set([row[0] for row in cursor]))
            except:
                log.error('Revert:%s: Failed to insert package management request into the database.',
                          itsm_id, exc_info=True)
                return jsonify({'success': False, 'error': 'Failed to communicate with the database.'}), 500
    except:
        log.error('Revert:%s: Failed to communicate with the database.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to communicate with the database.'}), 500

    # Start task to sync devices
    sync_devices()

    # Transition issue status to completed on Jira
    log.info('Revert:%s: Transitioning Jira issue status to completed.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Complete')
    except:
        log.error('Revert:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Send success response
    log.info('Revert:%s: Finished.', itsm_id)
    return jsonify({'success': True})


@app.route('/device/reboot', methods=['POST'])
def device_reboot():
    # Get request body
    body = request.get_json(force=True, silent=True)
    if body is None:
        return jsonify({'success': False, 'error': 'Expected a valid JSON request body.'}), 400

    try:
        # Get request values
        itsm_id = body.get('itsm_id')
        device_names = body.get('device_names', body.get('device_name', []))
        device_names = list(set([device_names] if isinstance(device_names, str) else device_names))

        # Validate request values
        if not itsm_id:
            return jsonify({'success': False, 'error': 'Expected ITSM ID in field \'itsm_id\'.'}), 400
        if not device_names:
            return jsonify({'success': False, 'error': 'Expected list of devices in field \'device_names\'.'}), 400
    except:
        return jsonify({'success': False, 'error': 'Invalid parameters.'}), 400

    log.info('Reboot:%s: Received request to reboot %s devices.', itsm_id, len(device_names))

    # Transition issue status to waiting on Jira
    log.info('Reboot:%s: Transitioning Jira issue status to waiting.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Wait')
    except:
        log.error('Reboot:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Run reboot action
    successes, failures = {}, []
    try:
        log.info('Reboot:%s: Connecting to SUSE Manager.', itsm_id)
        with SuseManager(SUSEMANAGER_URL, SUSEMANAGER_USERNAME, SUSEMANAGER_PASSWORD) as susemanager:
            log.info('Reboot:%s: Requesting reboot action from SUSE Manager.', itsm_id)
            action_ids = {}
            for device_name in device_names:
                try:
                    device_id = susemanager.get_device_id(device_name)
                    action_id = susemanager.exec('system', 'scheduleReboot', (device_id, datetime.datetime.now()))
                    if action_id is None:
                        log.error('Reboot:%s: Empty response while requesting reboot action for %s.',
                                  itsm_id, device_name, exc_info=True)
                        failures.append(device_name)
                        continue
                    action_ids[device_name] = action_id
                except:
                    log.error('Reboot:%s: Failed to request reboot action for %s.', itsm_id, device_name,
                              exc_info=True)
                    failures.append(device_name)
    except:
        log.error('Reboot:%s: Failed to connect to SUSE Manager.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to connect to SUSE Manager.'}), 500

    # Insert request data in the database
    log.info('Reboot:%s: Inserting reboot request into the database.', itsm_id)
    try:
        with psycopg2.connect(**POSTGRES_AUTH) as connection:
            for device_name, action_id in action_ids.items():
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(INSERT_REBOOT_REQUESTS_QUERY, (itsm_id, device_name, action_id))
                    successes[device_name] = action_id
                except:
                    log.error('Reboot:%s: Failed to insert reboot request for %s into the database.',
                              itsm_id, device_name, exc_info=True)
                    failures.append(device_name)
    except:
        log.error('Reboot:%s: Failed to communicate with the database.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to communicate with the database.'}), 500

    # Send response if there are any failures
    if failures:
        log.info('Reboot:%s: Finished with %s successes and %s failures.', itsm_id, len(successes), len(failures))
        return jsonify_clear({'success': False, 'successes': successes, 'failures': failures}), 500

    # Transition issue status to completed on Jira
    log.info('Reboot:%s: Transitioning Jira issue status to completed.', itsm_id)
    try:
        jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        jira.transition_issue(itsm_id, 'Complete')
    except:
        log.error('Reboot:%s: Failed to transition issue status on Jira.', itsm_id, exc_info=True)
        return jsonify({'success': False, 'error': 'Failed to transition issue status on Jira.'}), 500

    # Send success response
    log.info('Reboot:%s: Finished with %s successes and %s failures.', itsm_id, len(successes), len(failures))
    return jsonify_clear({'success': True, 'successes': successes, 'failures': failures})


@app.route('/device/sync', methods=['POST'])
def device_sync():
    sync_devices()
    return jsonify({'success': True})


@app.route('/data/sync', methods=['POST'])
def data_sync():
    sync_data()
    return jsonify({'success': True})


def sync_devices():
    thread = threading.Thread(target=sync_devices_thread, daemon=True)
    thread.start()


def sync_data():
    thread = threading.Thread(target=sync_data_thread, daemon=True)
    thread.start()


def sync_devices_thread():
    with SYNC_DEVICES_LOCK:
        log.info('Sync Devices: Received request to sync devices.')

        # Update data in the database
        log.info('Sync Devices: Reading list of package management requests from the database.')
        try:
            with psycopg2.connect(**POSTGRES_AUTH) as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(SELECT_PACKAGE_REQUESTS_QUERY)
                        package_requests = list(cursor)
                except:
                    log.error('Sync Devices: Failed to read package management requests from the database.',
                              exc_info=True)
                    return False
        except:
            log.error('Sync Devices: Failed to communicate with the database.', exc_info=True)
            return False

        # Pre-process package requests
        package_processed = {}
        package_reverted = set()

        for package_request in package_requests:
            device_name, package_name, package_version, reverted = package_request
            package_key = (device_name, package_name)
            if reverted:
                package_reverted.add((device_name, package_name))
            elif package_key not in package_processed:
                package_processed[package_key] = (device_name, package_name, package_version)

        # Pre-process reverted package requests
        if SUSEMANAGER_UNINSTALL_REVERTED:
            for package_request in package_reverted:
                if package_request not in package_processed:
                    package_processed[package_request] = package_request + (None,)

        package_requests = package_processed.values()

        # Run package management actions
        try:
            log.info('Sync Devices: Connecting to SUSE Manager.')
            with SuseManager(SUSEMANAGER_URL, SUSEMANAGER_USERNAME, SUSEMANAGER_PASSWORD) as susemanager:
                log.info('Sync Devices: Requesting package management actions from SUSE Manager.')
                for device_name, package_name, package_version in package_requests:
                    try:
                        device_id = susemanager.get_device_id(device_name)
                        if package_version:
                            package_ids = susemanager.get_package_ids(device_id, package_name, package_version)
                            if not package_ids:
                                continue
                            log.info('Sync Devices: Requesting installation of package %s at version %s on device %s '
                                     'from SUSE Manager.', package_name, package_version, device_name)
                            request_args = (device_id, package_ids[:1], datetime.datetime.now())
                            action_id = susemanager.exec('system', 'schedulePackageInstall', request_args)
                        else:
                            package_ids = susemanager.get_installed_package_ids(device_id, package_name)
                            if not package_ids:
                                continue
                            log.info('Sync Devices: Requesting removal of package %s on device %s from SUSE Manager.',
                                     package_name, device_name)
                            request_args = (device_id, package_ids[:1], datetime.datetime.now())
                            action_id = susemanager.exec('system', 'schedulePackageRemove', request_args)
                        if action_id is None:
                            log.error('Sync Devices: Empty response while requesting package management action for '
                                      '%s.', device_name, exc_info=True)
                    except:
                        log.error('Sync Devices: Failed to request package management action for %s.',
                                  device_name, exc_info=True)
        except:
            log.error('Sync Devices: Failed to connect to SUSE Manager.', exc_info=True)
            return False

        log.info('Sync Devices: Finished.')
        return True


def sync_data_thread():
    with SYNC_DATA_LOCK:
        log.info('Sync Data: Received request to sync data.')

        try:
            log.info('Sync Data: Connecting to SUSE Manager.')
            with SuseManager(SUSEMANAGER_URL, SUSEMANAGER_USERNAME, SUSEMANAGER_PASSWORD) as susemanager:
                log.info('Sync Data: Requesting list of devices from SUSE Manager.')
                try:
                    devices = {(device['id'], device['name']) for device in susemanager.exec('system', 'listSystems')}
                except:
                    log.error('Sync Data: Failed to fetch list of devices from SUSE Manager.', exc_info=True)
                    return False

                log.info('Sync Data: Requesting list of channels from SUSE Manager.')
                try:
                    channels = {channel['label'] for channel in susemanager.exec('channel', 'listSoftwareChannels')}
                except:
                    log.error('Sync Data: Failed to fetch list of channels from SUSE Manager.', exc_info=True)
                    return False

                log.info('Sync Data: Requesting list of packages from SUSE Manager.')
                try:
                    packages = set()
                    for channel in channels:
                        packages.update({
                            (package['id'], package['name'], package['version'])
                            for package in susemanager.exec('channel.software', 'listAllPackages', (channel,))
                        })
                except:
                    log.error('Sync Data: Failed to fetch list of packages from SUSE Manager.', exc_info=True)
                    return False
        except:
            log.error('Sync Data: Failed to connect to SUSE Manager.', exc_info=True)
            return False

        log.info('Sync Data: Inserting new data into the database.')
        try:
            with psycopg2.connect(**POSTGRES_AUTH) as connection:
                with connection.cursor() as cursor:
                    psycopg2.extras.execute_batch(cursor, INSERT_DEVICES_QUERY, devices)
                with connection.cursor() as cursor:
                    psycopg2.extras.execute_batch(cursor, INSERT_PACKAGES_QUERY, packages)
        except:
            log.error('Sync Data: Failed to communicate with the database.', exc_info=True)
            return False

        log.info('Sync Data: Reading all data from the database.')
        devices = []
        packages = {}
        packages_total = 0
        try:
            with psycopg2.connect(**POSTGRES_AUTH) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(SELECT_DEVICES_QUERY)
                    devices = [row[1] for row in cursor]
                with connection.cursor() as cursor:
                    cursor.execute(SELECT_PACKAGES_QUERY)
                    for _, package_name, package_version in cursor:
                        to_add = 1 if package_name in packages else 3
                        if packages_total + to_add >= JIRA.FIELD_OPTIONS_LIMIT:
                            break
                        packages_total += to_add
                        packages.setdefault(package_name, []).append(package_version)
        except:
            log.error('Sync Data: Failed to communicate with the database.', exc_info=True)
            return False

        log.info('Sync Data: Preparing data to be sent to Jira.')
        devices = sorted(devices)
        for package_name, package_versions in packages.items():
            tail = sorted(package_versions, key=split_version, reverse=True)
            packages[package_name] = ['Remove'] + [version for version in tail if version.lower() != 'remove']
        packages = dict(sorted(packages.items(), key=lambda item: item[0]))

        try:
            log.info('Sync Data: Getting custom fields from Jira.')
            jira = JIRA(JIRA_HOST, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
            fields = {field['name']: field for field in jira.fields()}

            # Populate devices
            devices_field = fields.get(JIRA_DEVICES_FIELD)
            if devices_field is None:
                devices_field = jira.create_custom_field(
                    name=JIRA_DEVICES_FIELD,
                    description='The name of the devices.',
                    type=CustomFieldType.MULTI_SELECT,
                    searcherKey=CustomFieldSearcherKey.MULTI_SELECT,
                )
            log.info('Sync Data: Clearing current field options for %s on Jira.', JIRA_DEVICES_FIELD)
            jira.clear_custom_field_options(devices_field['id'])
            log.info('Sync Data: Populating field options for %s on Jira.', JIRA_DEVICES_FIELD)
            jira.set_custom_field_options(devices_field['id'], devices)

            # Populate packages
            packages_field = fields.get(JIRA_PACKAGE_FIELD)
            if packages_field is None:
                packages_field = jira.create_custom_field(
                    name=JIRA_PACKAGE_FIELD,
                    description='The package and version to install, upgrade or downgrade to, or remove.',
                    type=CustomFieldType.CASCADING_SELECT,
                    searcherKey=CustomFieldSearcherKey.CASCADING_SELECT,
                )
            log.info('Sync Data: Clearing current field options for %s on Jira.', JIRA_PACKAGE_FIELD)
            jira.clear_custom_field_options(packages_field['id'])
            log.info('Sync Data: Populating field options for %s on Jira.', JIRA_PACKAGE_FIELD)
            jira.set_custom_field_options(packages_field['id'], packages)
        except:
            log.error('Sync Data: Failed to send data to Jira.', exc_info=True)
            return False

        log.info('Sync Data: Finished.')
        return True


def jsonify_clear(response):
    if 'successes' in response and not response['successes']:
        del response['successes']
    if 'failures' in response and not response['failures']:
        del response['failures']
    return response


def split_version(version):
    parts = RE_SPLIT_VERSION.split(version)
    for index, part in enumerate(parts):
        try:
            parts[index] = '{0:010d}'.format(int(part))
        except:
            pass
    return parts


def isoparse(timestamp):
    try:
        return dateutil.parser.isoparse(timestamp)
    except:
        pass
    return None


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_ENV') == 'development')
