# -*- coding: utf-8 -*-
#
# gPodder - A media aggregator and podcast client
# Copyright (c) 2005-2009 Thomas Perl and the gPodder Team
#
# gPodder is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# gPodder is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Loads and executes user extensions.

Extensions are Python scripts in "$GPODDER_HOME/Extensions". Each script must
define a class named "gPodderExtension", otherwise it will be ignored.

The extensions class defines several callbacks that will be called by gPodder
at certain points. See the methods defined below for a list of callbacks and
their parameters.

For an example extension see share/gpodder/examples/extensions.py
"""

import functools
import glob
import importlib
import logging
import os
import re

import gpodder
from gpodder import util

_ = gpodder.gettext


logger = logging.getLogger(__name__)


CATEGORY_DICT = {
    'desktop-integration': _('Desktop Integration'),
    'interface': _('Interface'),
    'post-download': _('Post download'),
}
DEFAULT_CATEGORY = _('Other')


def call_extensions(func):
    """Decorate a function to create handler in ExtensionManager.

    Calls the specified function in all user extensions that define it.
    """
    method_name = func.__name__

    @functools.wraps(func)
    def handler(self, *args, **kwargs):
        result = None
        for container in self.containers:
            if not container.enabled or container.module is None:
                continue

            try:
                callback = getattr(container.module, method_name, None)
                if callback is None:
                    continue

                # If the results are lists, concatenate them to show all
                # possible items that are generated by all extension together
                cb_res = callback(*args, **kwargs)
                if isinstance(result, list) and isinstance(cb_res, list):
                    result.extend(cb_res)
                elif cb_res is not None:
                    result = cb_res
            except Exception as exception:
                logger.error('Error in %s in %s: %s', container.filename,
                        method_name, exception, exc_info=True)
        func(self, *args, **kwargs)
        return result

    return handler


class ExtensionMetadata(object):
    # Default fallback metadata in case metadata fields are missing
    DEFAULTS = {
        'description': _('No description for this extension.'),
        'doc': None,
        'payment': None,
    }
    SORTKEYS = {
        'title': 1,
        'description': 2,
        'category': 3,
        'authors': 4,
        'only_for': 5,
        'mandatory_in': 6,
        'disable_in': 7,
    }

    def __init__(self, container, metadata):
        if 'title' not in metadata:
            metadata['title'] = container.name

        category = metadata.get('category', 'other')
        metadata['category'] = CATEGORY_DICT.get(category, DEFAULT_CATEGORY)

        self.__dict__.update(metadata)

    def __getattr__(self, name):
        try:
            return self.DEFAULTS[name]
        except KeyError as e:
            raise AttributeError(name, e)

    def get_sorted(self):

        def kf(x):
            return self.SORTKEYS.get(x[0], 99)

        return sorted([(k, v) for k, v in list(self.__dict__.items())], key=kf)

    def check_ui(self, target, default):
        """Check metadata information.

        Metadata information:
            __only_for__ = 'gtk'
            __mandatory_in__ = 'gtk'
            __disable_in__ = 'gtk'

        The metadata fields in an extension can be a string with
        comma-separated values for UIs. This will be checked against
        boolean variables in the "gpodder.ui" object.

        Example metadata field in an extension:

            __only_for__ = 'gtk'
            __only_for__ = 'unity'

        In this case, this function will return the value of the default
        if any of the following expressions will evaluate to True:

            gpodder.ui.gtk
            gpodder.ui.unity
            gpodder.ui.cli
            gpodder.ui.osx
            gpodder.ui.win32

        New, unknown UIs are silently ignored and will evaluate to False.
        """
        if not hasattr(self, target):
            return default

        uis = [_f for _f in [x.strip() for x in getattr(self, target).split(',')] if _f]
        return any(getattr(gpodder.ui, ui.lower(), False) for ui in uis)

    @property
    def available_for_current_ui(self):
        return self.check_ui('only_for', True)

    @property
    def mandatory_in_current_ui(self):
        return self.check_ui('mandatory_in', False)

    @property
    def disable_in_current_ui(self):
        return self.check_ui('disable_in', False)


class MissingDependency(Exception):
    def __init__(self, message, dependency, cause=None):
        Exception.__init__(self, message)
        self.dependency = dependency
        self.cause = cause


class MissingModule(MissingDependency):
    pass


class MissingCommand(MissingDependency):
    pass


class ExtensionContainer(object):
    """An extension container wraps one extension module."""

    def __init__(self, manager, name, config, filename=None, module=None):
        self.manager = manager

        self.name = name
        self.config = config
        self.filename = filename
        self.module = module
        self.enabled = False
        self.error = None

        self.default_config = None
        self.parameters = None
        self.metadata = ExtensionMetadata(self, self._load_metadata(filename))

    def require_command(self, command):
        """Check if the given command is installed on the system.

        Returns the complete path of the command

        @param command: String with the command name
        """
        result = util.find_command(command)
        if result is None:
            msg = _('Command not found: %(command)s') % {'command': command}
            raise MissingCommand(msg, command)
        return result

    def require_any_command(self, command_list):
        """Check if any of the given commands is installed on the system.

        Returns the complete path of first found command in the list

        @param command: List with the commands name
        """
        for command in command_list:
            result = util.find_command(command)
            if result is not None:
                return result

        msg = _('Need at least one of the following commands: %(list_of_commands)s') % \
            {'list_of_commands': ', '.join(command_list)}
        raise MissingCommand(msg, ', '.join(command_list))

    def _load_metadata(self, filename):
        if not filename or not os.path.exists(filename):
            return {}

        encoding = util.guess_encoding(filename)
        with open(filename, "r", encoding=encoding) as f:
            extension_py = f.read()
        metadata = dict(re.findall(r"__([a-z_]+)__ = '([^']+)'", extension_py))

        # Support for using gpodder.gettext() as _ to localize text
        localized_metadata = dict(re.findall(r"__([a-z_]+)__ = _\('([^']+)'\)",
            extension_py))

        for key in localized_metadata:
            metadata[key] = gpodder.gettext(localized_metadata[key])

        return metadata

    def set_enabled(self, enabled):
        if enabled and not self.enabled:
            try:
                self.load_extension()
                self.error = None
                self.enabled = True
                if hasattr(self.module, 'on_load'):
                    self.module.on_load()
            except Exception as exception:
                logger.error('Cannot load %s from %s: %s', self.name,
                        self.filename, exception, exc_info=True)
                if isinstance(exception, ImportError):
                    # Wrap ImportError in MissingCommand for user-friendly
                    # message (might be displayed in the GUI)
                    if exception.name:
                        module = exception.name
                        msg = _('Python module not found: %(module)s') % {
                            'module': module
                        }
                        exception = MissingCommand(msg, module, exception)
                self.error = exception
                self.enabled = False
        elif not enabled and self.enabled:
            try:
                if hasattr(self.module, 'on_unload'):
                    self.module.on_unload()
            except Exception as exception:
                logger.error('Failed to on_unload %s: %s', self.name,
                        exception, exc_info=True)
            self.enabled = False

    def load_extension(self):
        """Load and initialize the gPodder extension module."""
        if self.module is not None:
            logger.info('Module already loaded.')
            return

        if not self.metadata.available_for_current_ui:
            logger.info('Not loading "%s" (only_for = "%s")',
                    self.name, self.metadata.only_for)
            return

        basename, _ = os.path.splitext(os.path.basename(self.filename))
        try:
            # from load_source() on https://docs.python.org/dev/whatsnew/3.12.html
            loader = importlib.machinery.SourceFileLoader(basename, self.filename)
            spec = importlib.util.spec_from_file_location(basename, self.filename, loader=loader)
            module_file = importlib.util.module_from_spec(spec)
            loader.exec_module(module_file)
        finally:
            # Remove the .pyc file if it was created during import
            util.delete_file(self.filename + 'c')

        self.default_config = getattr(module_file, 'DefaultConfig', {})
        if self.default_config:
            self.manager.core.config.register_defaults({
                'extensions': {
                    self.name: self.default_config,
                }
            })
        self.config = getattr(self.manager.core.config.extensions, self.name)

        self.module = module_file.gPodderExtension(self)
        logger.info('Module loaded: %s', self.filename)


class ExtensionManager(object):
    """Loads extensions and manages self-registering plugins."""

    def __init__(self, core):
        self.core = core
        self.filenames = os.environ.get('GPODDER_EXTENSIONS', '').split()
        self.containers = []

        core.config.add_observer(self._config_value_changed)
        enabled_extensions = core.config.extensions.enabled

        if os.environ.get('GPODDER_DISABLE_EXTENSIONS', '') != '':
            logger.info('Disabling all extensions (from environment)')
            return

        for name, filename in self._find_extensions():
            logger.debug('Found extension "%s" in %s', name, filename)
            config = getattr(core.config.extensions, name)
            container = ExtensionContainer(self, name, config, filename)
            if (name in enabled_extensions
                    or container.metadata.mandatory_in_current_ui):
                container.set_enabled(True)
            if (name in enabled_extensions
                    and container.metadata.disable_in_current_ui):
                container.set_enabled(False)
            self.containers.append(container)

    def shutdown(self):
        for container in self.containers:
            container.set_enabled(False)

    def _config_value_changed(self, name, old_value, new_value):
        if name != 'extensions.enabled':
            return

        for container in self.containers:
            new_enabled = (container.name in new_value)
            if new_enabled == container.enabled:
                continue
            if not new_enabled and container.metadata.mandatory_in_current_ui:
                # forced extensions are never listed in extensions.enabled
                continue

            logger.info('Extension "%s" is now %s', container.name,
                    'enabled' if new_enabled else 'disabled')
            container.set_enabled(new_enabled)
            if new_enabled and not container.enabled:
                logger.warning('Could not enable extension: %s',
                        container.error)
                self.core.config.extensions.enabled = [x
                        for x in self.core.config.extensions.enabled
                        if x != container.name]

    def _find_extensions(self):
        extensions = {}

        if not self.filenames:
            builtins = os.path.join(gpodder.prefix, 'share', 'gpodder',
                'extensions', '*.py')
            user_extensions = os.path.join(gpodder.home, 'Extensions', '*.py')
            self.filenames = glob.glob(builtins) + glob.glob(user_extensions)

        # Let user extensions override built-in extensions of the same name.
        # This inherently happens because we search the user extensions folder second,
        # and the entries are put in the extensions dict by their name field.
        for filename in self.filenames:
            if not filename or not os.path.exists(filename):
                logger.info('Skipping non-existing file: %s', filename)
                continue

            name, _ = os.path.splitext(os.path.basename(filename))

            # strip ordering prefix, if present
            name = re.sub(r'^[0-9]*_', '', name)
            extensions[name] = filename

        # sort by filename
        return sorted(extensions.items(), key=lambda i: i[1])

    def get_extensions(self):
        """Get a list of all loaded extensions and their enabled flag."""
        return [c for c in self.containers
            if c.metadata.available_for_current_ui
            and not c.metadata.mandatory_in_current_ui
            and not c.metadata.disable_in_current_ui]

    # Define all known handler functions here, decorate them with the
    # "call_extension" decorator to forward all calls to extension scripts that have
    # the same function defined in them. If the handler functions here contain
    # any code, it will be called after all the extensions have been called.

    @call_extensions
    def on_ui_initialized(self, model, update_podcast_callback,
            download_episode_callback):
        """Called when the user interface is initialized.

        @param model: A gpodder.model.Model instance
        @param update_podcast_callback: Function to update a podcast feed
        @param download_episode_callback: Function to download an episode
        """  # noqa: D401

    @call_extensions
    def on_podcast_subscribe(self, podcast):
        """Called when the user subscribes to a new podcast feed.

        @param podcast: A gpodder.model.PodcastChannel instance
        """  # noqa: D401

    @call_extensions
    def on_podcast_updated(self, podcast):
        """Called when a podcast feed was updatedi.

        This extension will be called even if there were no new episodes.

        @param podcast: A gpodder.model.PodcastChannel instance
        """  # noqa: D401

    @call_extensions
    def on_podcast_update_failed(self, podcast, exception):
        """Called when a podcast update failed.

        @param podcast: A gpodder.model.PodcastChannel instance

        @param exception: The reason.
        """  # noqa: D401

    @call_extensions
    def on_podcast_save(self, podcast):
        """Called when a podcast is saved to the database.

        This extensions will be called when the user edits the metadata of
        the podcast or when the feed was updated.

        @param podcast: A gpodder.model.PodcastChannel instance
        """  # noqa: D401

    @call_extensions
    def on_podcast_delete(self, podcast):
        """Called when a podcast is deleted from the database.

        @param podcast: A gpodder.model.PodcastChannel instance
        """  # noqa: D401

    @call_extensions
    def on_episode_playback(self, episode):
        """Called when an episode is played back.

        This function will be called when the user clicks on "Play" or
        "Open" in the GUI to open an episode with the media player.

        @param episode: A gpodder.model.PodcastEpisode instance
        """  # noqa: D401

    @call_extensions
    def on_episode_save(self, episode):
        """Called when an episode is saved to the database.

        This extension will be called when a new episode is added to the
        database or when the state of an existing episode is changed.

        @param episode: A gpodder.model.PodcastEpisode instance
        """  # noqa: D401

    @call_extensions
    def on_episode_downloaded(self, episode):
        """Called when an episode has been downloaded.

        You can retrieve the filename via episode.local_filename(False)

        @param episode: A gpodder.model.PodcastEpisode instance
        """  # noqa: D401

    @call_extensions
    def on_all_episodes_downloaded(self):
        """Called when all episodes has been downloaded."""  # noqa: D401

    @call_extensions
    def on_episode_synced(self, device, episode):
        """Called when an episode has been synced to device.

        You can retrieve the filename via episode.local_filename(False)
        For MP3PlayerDevice:
            You can retrieve the filename on device via
                device.get_episode_file_on_device(episode)
            You can retrieve the folder name on device via
                device.get_episode_folder_on_device(episode)

        @param device: A gpodder.sync.Device instance
        @param episode: A gpodder.model.PodcastEpisode instance
        """  # noqa: D401

    @call_extensions
    def on_all_episodes_synced(self):
        """Called when all episodes have been synchronized
        """

    @call_extensions
    def on_create_menu(self):
        """Called when the Extras menu is created.

        You can add additional Extras menu entries here. You have to return a
        list of tuples, where the first item is a label and the second item is a
        callable that will get no parameter.

        Example return value:

        [('Sync to Smartphone', lambda : ...)]
        """  # noqa: D401

    @call_extensions
    def on_episodes_context_menu(self, episodes):
        """Called when the episode list context menu is opened.

        You can add additional context menu entries here. You have to
        return a list of tuples, where the first item is a label and
        the second item is a callable that will get the episode as its
        first and only parameter.

        Example return value:

        [('Mark as new', lambda episodes: ...)]

        @param episodes: A list of gpodder.model.PodcastEpisode instances
        """  # noqa: D401

    @call_extensions
    def on_channel_context_menu(self, channel):
        """Called when the channel list context menu is opened.

        You can add additional context menu entries here. You have to return a
        list of tuples, where the first item is a label and the second item is a
        callable that will get the channel as its first and only parameter.

        Example return value:

        [('Update channel', lambda channel: ...)]
        @param channel: A gpodder.model.PodcastChannel instance
        """  # noqa: D401

    @call_extensions
    def on_episode_delete(self, episode, filename):
        """Called before the episode's disk file is about to be deleted."""  # noqa: D401

    @call_extensions
    def on_episode_removed_from_podcast(self, episode):
        """Called before the episode is about to be removed from a channel.

        E.g., when the episode has not been downloaded and it disappears from the feed.

        @param podcast: A gpodder.model.PodcastChannel instance
        """  # noqa: D401

    @call_extensions
    def on_notification_show(self, title, message):
        """Called when a notification should be shown.

        @param title: title of the notification
        @param message: message of the notification
        """  # noqa: D401

    @call_extensions
    def on_download_progress(self, progress):
        """Called when the overall download progress changes.

        @param progress: The current progress value (0..1)
        """  # noqa: D401

    @call_extensions
    def on_ui_object_available(self, name, ui_object):
        """Called when an UI-specific object becomes available.

        XXX: Experimental. This hook might go away without notice (and be
        replaced with something better). Only use for in-tree extensions.

        @param name: The name/ID of the object
        @param ui_object: The object itself
        """  # noqa: D401

    @call_extensions
    def on_application_started(self):
        """Called when the application started.

        This is for extensions doing stuff at startup that they don't
        want to do if they have just been enabled.
        e.g. minimize at startup should not minimize the application when
        enabled but only on following startups.

        It is called after on_ui_object_available and on_ui_initialized.
        """  # noqa: D401

    @call_extensions
    def on_find_partial_downloads_done(self):
        """Called when the application started and the lookout for resume is done.

        This is mainly for extensions scheduling refresh or downloads at startup,
        to prevent race conditions with the find_partial_downloads method.

        It is called after on_application_started.
        """  # noqa: D401

    @call_extensions
    def on_preferences(self):
        """Called when the preferences dialog is opened.

        You can add additional tabs to the preferences dialog here. You have to
        return a list of tuples, where the first item is a label and the second
        item is a callable with no parameters and returns a Gtk widget.

        Example return value:

        [('Tab name', lambda: ...)]
        """  # noqa: D401

    @call_extensions
    def on_channel_settings(self, channel):
        """Called when a channel settings dialog is opened.

        You can add additional tabs to the channel settings dialog here. You
        have to return a list of tuples, where the first item is a label and the
        second item is a callable that will get the channel as its first and
        only parameter and returns a Gtk widget.

        Example return value:

        [('Tab name', lambda channel: ...)]

        @param channel: A gpodder.model.PodcastChannel instance
        """  # noqa: D401
