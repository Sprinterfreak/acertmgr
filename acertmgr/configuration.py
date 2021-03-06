#!/usr/bin/env python
# -*- coding: utf-8 -*-

# config - acertmgr config parser
# Copyright (c) Markus Hauschild & David Klaftenegger, 2016.
# Copyright (c) Rudolf Mayerhofer, 2019.
# available under the ISC license, see LICENSE

import argparse
import copy
import hashlib
import io
import os

# Backward compatiblity for older versions/installations of acertmgr
LEGACY_WORK_DIR = "/etc/acme"
LEGACY_CONF_FILE = os.path.join(LEGACY_WORK_DIR, "acme.conf")
LEGACY_CONF_DIR = os.path.join(LEGACY_WORK_DIR, "domains.d")

# Configuration defaults to use if not specified otherwise
DEFAULT_CONF_FILE = "/etc/acertmgr/acertmgr.conf"
DEFAULT_CONF_DIR = "/etc/acertmgr"
DEFAULT_KEY_LENGTH = 4096  # bits
DEFAULT_TTL = 15  # days
DEFAULT_API = "v1"
DEFAULT_AUTHORITY = "https://acme-v01.api.letsencrypt.org"
DEFAULT_AUTHORITY_AGREEMENT = "https://letsencrypt.org/documents/LE-SA-v1.2-November-15-2017.pdf"


# @brief augment configuration with defaults
# @param domainconfig the domain configuration
# @param defaults the default configuration
# @return the augmented configuration
def complete_action_config(domainconfig, config):
    defaults = config['defaults']
    domainconfig['ca_file'] = config['ca_file']
    domainconfig['cert_file'] = config['cert_file']
    domainconfig['key_file'] = config['key_file']
    for name, value in defaults.items():
        if name not in domainconfig:
            domainconfig[name] = value
    if 'action' not in domainconfig:
        domainconfig['action'] = None
    return domainconfig


# @brief update config[name] with value from localconfig>globalconfig>default
def update_config_value(config, name, localconfig, globalconfig, default):
    values = [x for x in localconfig if name in x]
    if len(values) > 0:
        config[name] = values[0]
    else:
        config[name] = globalconfig.get(name, default)


# @brief load the configuration from a file
def parse_config_entry(entry, globalconfig, work_dir):
    config = dict()

    # Basic domain information
    config['domains'], data = entry
    config['domainlist'] = config['domains'].split(' ')
    config['id'] = hashlib.md5(config['domains'].encode('utf-8')).hexdigest()

    # Action config defaults
    config['defaults'] = globalconfig.get('defaults', {})

    # API version
    update_config_value(config, 'api', entry, globalconfig, DEFAULT_API)

    # Certificate authority
    update_config_value(config, 'authority', entry, globalconfig, DEFAULT_AUTHORITY)

    # Certificate authority agreement
    update_config_value(config, 'authority_agreement', entry, globalconfig, DEFAULT_AUTHORITY_AGREEMENT)

    # Account key
    update_config_value(config, 'account_key', entry, globalconfig, os.path.join(work_dir, "account.key"))

    # Certificate directory
    update_config_value(config, 'cert_dir', entry, globalconfig, work_dir)

    # TTL days
    update_config_value(config, 'ttl_days', entry, globalconfig, DEFAULT_TTL)

    # SSL cert location (with compatibility to older versions)
    update_config_value(config, 'cert_file', entry, globalconfig,
                        globalconfig.get('server_cert',
                                         os.path.join(config['cert_dir'], "{}.crt".format(config['id']))))

    # SSL key location (with compatibility to older versions)
    update_config_value(config, 'key_file', entry, globalconfig,
                        globalconfig.get('server_key',
                                         os.path.join(config['cert_dir'], "{}.key".format(config['id']))))

    # SSL key length (if key has to be (re-)generated, converted to int)
    update_config_value(config, 'key_length', entry, globalconfig, DEFAULT_KEY_LENGTH)
    config['key_length'] = int(config['key_length'])

    # SSL CA location
    ca_files = [x for x in entry if 'ca_file' in x]
    if len(ca_files) > 0:
        config['static_ca'] = True
        config['ca_file'] = ca_files[0]
    elif 'server_ca' in globalconfig:
        config['static_ca'] = True
        config['ca_file'] = globalconfig['server_ca']
    else:
        config['static_ca'] = False
        config['ca_file'] = os.path.join(config['cert_dir'], "{}.ca".format(config['id']))

    # Domain action configuration
    config['actions'] = list()
    for actioncfg in [x for x in data if 'path' in x]:
        config['actions'].append(complete_action_config(actioncfg, config))

    # Domain challenge handler configuration
    config['handlers'] = dict()
    handlerconfigs = [x for x in data if 'mode' in x]
    for domain in config['domainlist']:
        # Use global config as base handler config
        cfg = copy.deepcopy(globalconfig)

        # Determine generic domain handler config values
        genericfgs = [x for x in handlerconfigs if 'domain' not in x]
        if len(genericfgs) > 0:
            cfg.update(genericfgs[0])

        # Update handler config with more specific values
        specificcfgs = [x for x in handlerconfigs if ('domain' in x and x['domain'] == domain)]
        if len(specificcfgs) > 0:
            cfg.update(specificcfgs[0])

        config['handlers'][domain] = cfg

    return config


# @brief load the configuration from a file
def load():
    parser = argparse.ArgumentParser(description="acertmgr - Automated Certificate Manager using ACME/Let's Encrypt")
    parser.add_argument("-c", "--config-file", nargs="?",
                        help="global configuration file (default='{}')".format(DEFAULT_CONF_FILE))
    parser.add_argument("-d", "--config-dir", nargs="?",
                        help="domain configuration directory (default='{}')".format(DEFAULT_CONF_DIR))
    parser.add_argument("-w", "--work-dir", nargs="?",
                        help="persistent work data directory (default=config_dir)")
    args = parser.parse_args()

    # Determine global configuration file
    if args.config_file:
        global_config_file = args.config_file
    elif os.path.isfile(LEGACY_CONF_FILE):
        global_config_file = LEGACY_CONF_FILE
    else:
        global_config_file = DEFAULT_CONF_FILE

    # Determine domain configuration directory
    if args.config_dir:
        domain_config_dir = args.config_dir
    elif os.path.isdir(LEGACY_CONF_DIR):
        domain_config_dir = LEGACY_CONF_DIR
    else:
        domain_config_dir = DEFAULT_CONF_DIR

    # Determine work directory...
    if args.work_dir:
        work_dir = args.work_dir
    elif os.path.isdir(LEGACY_WORK_DIR):
        work_dir = LEGACY_WORK_DIR
    else:
        # .. or use the domain configuration directory otherwise
        work_dir = domain_config_dir

    # load global configuration
    globalconfig = dict()
    if os.path.isfile(global_config_file):
        with io.open(global_config_file) as config_fd:
            try:
                import json
                globalconfig = json.load(config_fd)
            except ValueError:
                import yaml
                config_fd.seek(0)
                globalconfig = yaml.safe_load(config_fd)

    # create work directory if it does not exist
    if not os.path.isdir(work_dir):
        os.mkdir(work_dir, int("0700", 8))

    # load domain configuration
    config = list()
    if os.path.isdir(domain_config_dir):
        for domain_config_file in os.listdir(domain_config_dir):
            # check file extension and skip if global config file
            if domain_config_file.endswith(".conf") and domain_config_file != global_config_file:
                with io.open(os.path.join(domain_config_dir, domain_config_file)) as config_fd:
                    try:
                        import json
                        for entry in json.load(config_fd).items():
                            config.append(parse_config_entry(entry, globalconfig, work_dir))
                    except ValueError:
                        import yaml
                        config_fd.seek(0)
                        for entry in yaml.safe_load(config_fd).items():
                            config.append(parse_config_entry(entry, globalconfig, work_dir))

    return config
