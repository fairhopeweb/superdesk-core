#!/usr/bin/env python
# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014, 2015 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from typing import Dict, Any, Type

import os
import eve
import flask
import jinja2
import importlib
import superdesk
import logging

from flask_mail import Mail
from eve.auth import TokenAuth
from eve.io.mongo.mongo import _create_index as create_index
from eve.io.media import MediaStorage
from eve.render import send_response
from flask_babel import Babel
from flask import g, json
from babel import parse_locale
from pymongo.errors import DuplicateKeyError

from superdesk.celery_app import init_celery
from superdesk.datalayer import SuperdeskDataLayer  # noqa
from superdesk.errors import SuperdeskError, SuperdeskApiError
from superdesk.factory.sentry import SuperdeskSentry
from superdesk.logging import configure_logging
from superdesk.storage import ProxyMediaStorage
from superdesk.validator import SuperdeskValidator
from superdesk.json_utils import SuperdeskJSONEncoder
from .elastic_apm import setup_apm

SUPERDESK_PATH = os.path.abspath(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

logger = logging.getLogger(__name__)


def set_error_handlers(app):
    """Set error handlers for the given application object.

    Each error handler receives a :py:class:`superdesk.errors.SuperdeskError`
    instance as a parameter and returns a tuple containing an error message
    that is sent to the client and the HTTP status code.

    :param app: an instance of `Eve <http://python-eve.org/>`_ application
    """

    @app.errorhandler(SuperdeskError)
    def client_error_handler(error):
        error_dict = error.to_dict()
        error_dict.update(internal_error=error.status_code)
        status_code = error.status_code or 422
        return send_response(None, (error_dict, None, None, status_code))

    @app.errorhandler(403)
    def server_forbidden_handler(error):
        return send_response(None, ({"code": 403, "error": error.response}, None, None, 403))

    @app.errorhandler(AssertionError)
    def assert_error_handler(error):
        return send_response(None, ({"code": 400, "error": str(error) if str(error) else "assert"}, None, None, 400))

    @app.errorhandler(500)
    def server_error_handler(error):
        """Log server errors."""
        return_error = SuperdeskApiError.internalError(error)
        return client_error_handler(return_error)


class SuperdeskEve(eve.Eve):
    def __init__(self, **kwargs):
        # set attributes to avoid event slots being created
        # when getattr is called on those, thx to eve
        self.apm = None
        self.babel_tzinfo = None
        self.babel_locale = None
        self.babel_translations = None
        self.notification_client = None

        super().__init__(**kwargs)

    def __getattr__(self, name):
        """Workaround for https://github.com/pyeve/eve/issues/1087"""
        if name in {"im_self", "im_func"}:
            raise AttributeError("type object '%s' has no attribute '%s'" % (self.__class__.__name__, name))
        return super(SuperdeskEve, self).__getattr__(name)

    def init_indexes(self, ignore_duplicate_keys=False):
        for resource, resource_config in self.config["DOMAIN"].items():
            mongo_indexes = resource_config.get("mongo_indexes__init")
            if not mongo_indexes:
                continue

            # Borrowed https://github.com/pyeve/eve/blob/22ea4bfebc8b633251cd06837893ff699bd07a00/eve/flaskapp.py#L915
            for name, value in mongo_indexes.items():
                if isinstance(value, tuple):
                    list_of_keys, index_options = value
                else:
                    list_of_keys = value
                    index_options = {}

                # index creation in background
                index_options.setdefault("background", True)

                try:
                    create_index(self, resource, name, list_of_keys, index_options)
                except KeyError:
                    logger.warning("resource config missing for %s", resource)
                    continue
                except DuplicateKeyError as err:
                    # Duplicate key for unique indexes are generally caused by invalid documents in the collection
                    # such as multiple documents not having a value for the attribute used for the index
                    # Log the error so it can be diagnosed and fixed
                    logger.exception(err)

                    if not ignore_duplicate_keys:
                        raise

    def item_scope(self, name, schema=None):
        """Register item scope."""
        self.config.setdefault("item_scope", {})[name] = {
            "schema": schema,
        }

        def update_resource_schema(resource):
            assert schema
            self.config["DOMAIN"][resource]["schema"].update(schema)
            for key in schema:
                self.config["DOMAIN"][resource]["datasource"]["projection"][key] = 1

        if schema is not None:
            for resource in ("archive", "archive_autosave", "published", "archived"):
                update_resource_schema(resource)
                versioned_resource = resource + self.config["VERSIONS"]
                if versioned_resource in self.config["DOMAIN"]:
                    update_resource_schema(versioned_resource)


def get_media_storage_class(app_config: Dict[str, Any], use_provider_config: bool = True) -> Type[MediaStorage]:
    if use_provider_config and app_config.get("MEDIA_STORAGE_PROVIDER"):
        if isinstance(app_config["MEDIA_STORAGE_PROVIDER"], str):
            module_name, class_name = app_config["MEDIA_STORAGE_PROVIDER"].rsplit(".", 1)
            module = importlib.import_module(module_name)
            klass = getattr(module, class_name)
            if not issubclass(klass, MediaStorage):
                raise SystemExit("Invalid setting MEDIA_STORAGE_PROVIDER. Class must extend eve.io.media.MediaStorage")
            return klass

    return ProxyMediaStorage


def get_app(config=None, media_storage=None, config_object=None, init_elastic=None):
    """App factory.

    :param config: configuration that can override config from ``default_settings.py``
    :param media_storage: media storage class to use
    :param config_object: config object to load (can be module name, module or an object)
    :param init_elastic: obsolete config - kept there for BC
    :return: a new SuperdeskEve app instance
    """

    abs_path = SUPERDESK_PATH
    app_config = flask.Config(abs_path)
    app_config.from_object("superdesk.default_settings")
    app_config.setdefault("APP_ABSPATH", abs_path)
    app_config.setdefault("DOMAIN", {})
    app_config.setdefault("SOURCES", {})

    if config_object:
        app_config.from_object(config_object)

    try:
        app_config.update(config or {})
    except TypeError:
        app_config.from_object(config)

    if not media_storage:
        media_storage = get_media_storage_class(app_config)

    app = SuperdeskEve(
        data=SuperdeskDataLayer,
        auth=TokenAuth,
        media=media_storage,
        settings=app_config,
        json_encoder=SuperdeskJSONEncoder,
        validator=SuperdeskValidator,
        template_folder=os.path.join(abs_path, "templates"),
    )

    app.jinja_options = {"autoescape": False}
    app.json_encoder = SuperdeskJSONEncoder  # seems like eve param doesn't set it on flask

    # init client_config with default config
    app.client_config = {
        "content_expiry_minutes": app.config.get("CONTENT_EXPIRY_MINUTES", 0),
        "ingest_expiry_minutes": app.config.get("INGEST_EXPIRY_MINUTES", 0),
    }

    superdesk.app = app

    custom_loader = jinja2.ChoiceLoader(
        [
            jinja2.FileSystemLoader("templates"),
            jinja2.FileSystemLoader(os.path.join(SUPERDESK_PATH, "templates")),
        ]
    )

    app.jinja_loader = custom_loader
    app.mail = Mail(app)
    app.sentry = SuperdeskSentry(app)
    setup_apm(app)

    # setup babel
    app.config.setdefault("BABEL_TRANSLATION_DIRECTORIES", os.path.join(SUPERDESK_PATH, "translations"))
    babel = Babel(app, configure_jinja=False)

    @babel.localeselector
    def get_locale():
        user = getattr(g, "user", {})
        user_language = user.get("language", app.config.get("DEFAULT_LANGUAGE", "en"))
        try:
            # Attempt to load the local using Babel.parse_local
            parse_locale(user_language.replace("-", "_"))
        except ValueError:
            # If Babel fails to recognise the locale, then use the default language
            user_language = app.config.get("DEFAULT_LANGUAGE", "en")

        return user_language.replace("-", "_")

    set_error_handlers(app)

    @app.after_request
    def after_request(response):
        # fixing previous media prefixes if defined
        if app.config["MEDIA_PREFIXES_TO_FIX"] and app.config["MEDIA_PREFIX"]:
            current_prefix = app.config["MEDIA_PREFIX"].rstrip("/").encode()
            for prefix in app.config["MEDIA_PREFIXES_TO_FIX"]:
                response.data = response.data.replace(prefix.rstrip("/").encode(), current_prefix)
        return response

    init_celery(app)
    installed = set()

    def install_app(module_name):
        if module_name in installed:
            return
        installed.add(module_name)
        app_module = importlib.import_module(module_name)
        if hasattr(app_module, "init_app"):
            app_module.init_app(app)

    for module_name in app.config.get("CORE_APPS", []):
        install_app(module_name)

    for module_name in app.config.get("INSTALLED_APPS", []):
        install_app(module_name)

    app.config.setdefault("DOMAIN", {})
    for resource in superdesk.DOMAIN:
        if resource not in app.config["DOMAIN"]:
            app.register_resource(resource, superdesk.DOMAIN[resource])

    for name, jinja_filter in superdesk.JINJA_FILTERS.items():
        app.jinja_env.filters[name] = jinja_filter

    configure_logging(app.config["LOG_CONFIG_FILE"])

    return app
