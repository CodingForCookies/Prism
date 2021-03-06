import base64
import inspect
import json
import os
import pip
import platform
import re
import sys
import subprocess

import subprocess
from subprocess import PIPE

import flask
from flask import Blueprint
from flask_menu import current_menu
from flask_sqlalchemy import SQLAlchemy
import jinja2

from prism.config import JSONConfig
import prism.settings
import prism.api.plugin
import prism.api.view
import prism.logging as logging


from prism.version import get_version
__version__ = get_version()

PRISM_STATE = None

def init(flask_app, config):
	global PRISM_STATE
	PRISM_STATE = Prism(flask_app, config)

	import prism.login
	import prism.views

	PRISM_STATE.database.create_all()

def get():
	return PRISM_STATE

def flask_app():
	return PRISM_STATE.flask_app()

def plugin_manager():
	if PRISM_STATE._plugin_manager is None:
		PRISM_STATE._plugin_manager = PluginManager(PRISM_STATE.config)
		PRISM_STATE.plugin_manager.init()
	return PRISM_STATE.plugin_manager

def get_plugin(plugin_id):
	return PRISM_STATE.plugin_manager.get_plugin(plugin_id)

class Prism:
	def __init__(self, flask_app, config):
		self._flask_app = flask_app
		self.config = config

		self._plugin_manager = None

		self._flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///%s' % os.path.join(prism.settings.CONFIG_FOLDER, 'prism.db')
		self._flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
		self._database = SQLAlchemy(self._flask_app)

	def flask_app(self):
		""" Returns the flask application instance """
		return self._flask_app

	@property
	def plugin_manager(self):
		""" Returns the plugin manager instance """
		return self._plugin_manager

	@property
	def database(self):
		return self._database

class PluginManager:
	def __init__(self, config):
		self.available_plugins = {}
		self.plugins = {}

		# Holds the list of enabled plugins
		self.enabled_plugins = config.get('enabled_plugins', [])

		self.possible_permissions = {}

	def init(self):
		logging.up('Searching')
		self._search_plugins(settings.CORE_PLUGINS_PATH, True)
		self._search_plugins(settings.PLUGINS_PATH, False)
		logging.down()

		self._verify_dependencies()

		logging.up('Loading plugins')
		self._load_plugins()
		logging.down()

	def get_sorted_plugins(self):
		sorted_keys = sorted(self.plugins, key=lambda x: x)

		j = 0
		for i, plugin_id in enumerate(sorted_keys):
			if self.plugins[plugin_id].is_core:
				sorted_keys.insert(j, sorted_keys.pop(i))
				j += 1

		sorted_plugins = list()
		for plugin_id in sorted_keys:
			sorted_plugins.append(self.plugins[plugin_id])

		return sorted_plugins

	def get_plugin(self, plugin_id):
		""" Get a plugin, loaded or not """
		if plugin_id in self.plugins:
			return self.plugins[plugin_id]
		return None

	def is_enabled(self, plugin_id):
		""" Returns true if and only if all the plugin's dependencies are satisfied and
		it's set it enabled """
		return plugin_id in self.enabled_plugins

	def is_satisfied(self, plugin_id):
		""" Returns true if the plugin's dependencies are satisfied """
		return self.plugins[plugin_id].is_satisfied

	def find_classes(self, the_class):
		for plugin_id, plugin in self.plugins.items():
			for name, obj in self.get_classes(plugin._module, the_class):
				yield (plugin_id, plugin, name, obj)

	def get_classes(self, module, search_class):
		for name, obj in inspect.getmembers(module):
			if inspect.isclass(obj):
				# Search for the plugin's base class
				if obj != search_class and issubclass(obj, search_class):
					yield (name, obj)

	def _insert_dummy_plugin(self, plugin_info):
		dummy = prism.api.plugin.BasePlugin()
		dummy._info = plugin_info
		self.plugins[dummy.plugin_id] = dummy

	def _search_plugins(self, path, is_core):
		""" Searches for plugins in a specified folder """
		if is_core:
			logging.info('Finding core plugins')
		else:
			logging.info('Finding additional plugins')

		sys.path.append(path)

		for plugin_id in os.listdir(path):
			if not plugin_id.startswith('prism_'):
				continue

			base_folder = os.path.join(path, plugin_id)
			if not os.path.isfile(base_folder):
				if not os.path.exists(os.path.join(base_folder, 'plugin.json')):
					logging.error('Plugin does not have a plugin.json file. Offender: %s' % plugin_id)
					continue

				plugin_info = JSONConfig(base_folder, 'plugin.json', auto_save=False)
				plugin_info['_id'] = plugin_id
				plugin_info['id'] = plugin_info['_id'].split('_', 1)[1]
				plugin_info['_is_core'] = is_core
				plugin_info['_is_satisfied'] = True
				plugin_info['_is_enabled'] = False

				# Make the version readable
				version = None
				for i in plugin_info['version']:
					if isinstance(i, int):
						if version is None:
							version = str(i)
						else:
							version += '.' + str(i)
					else:
						version += '-' + i
				plugin_info['_version'] = plugin_info['version']
				plugin_info['version'] = version

				plugin_info['_dependencies'] = list()

				self.available_plugins[plugin_id] = plugin_info

	def _verify_dependencies(self):
		# These will always be initialized.
		logging.up('Verifying dependencies')
		installed_packages_list = sorted([i.key for i in pip.get_installed_distributions()])
		for plugin_id, plugin_info in self.available_plugins.items():
			if not plugin_info['_is_core']:
				if 'dependencies' not in plugin_info:
					continue

				if 'os' in plugin_info['dependencies']:
					if get_general_os() in plugin_info['dependencies']['os']:
						plugin_info['dependencies'] = data_merge(plugin_info['dependencies'], plugin_info['dependencies']['os'][get_general_os()])

				if 'plugin' in plugin_info['dependencies']:
					for depend_name in plugin_info['dependencies']['plugin']:
						installed = 'prism_' + depend_name in self.available_plugins
						if not installed:
							plugin_info['_is_satisfied'] = False
						plugin_info['_dependencies'].append(('plugin', depend_name, installed))

				if 'binary' in plugin_info['dependencies']:
					for depend_name in plugin_info['dependencies']['binary']:
						installed = is_package_installed(depend_name)
						if not installed:
							plugin_info['_is_satisfied'] = False
						plugin_info['_dependencies'].append(('binary', depend_name, installed))

				if 'module' in plugin_info['dependencies']:
					for depend_name in plugin_info['dependencies']['module']:
						installed = (depend_name in installed_packages_list)
						if not installed:
							plugin_info['_is_satisfied'] = False
						plugin_info['_dependencies'].append(('module', depend_name, installed))

				if not plugin_info['_is_satisfied']:
					# Create a dummy plugin container
					self._insert_dummy_plugin(plugin_info)
					logging.error('Dependency unsatisfied. Offender: %s' % plugin_id)
		logging.down()

	def _load_plugins(self):
		""" Attempts to load every enabled plugin """
		plugins_loaded = list()

		core_plugins = list()
		plugins_additional = list()

		for plugin_id, plugin_info in self.available_plugins.items():
			if plugin_info['_is_core']:
				core_plugins.append(plugin_info)
			elif plugin_info['_is_satisfied']:
				plugins_additional.append(plugin_info)

		# These will always be initialized.
		logging.info('Loading %s core plugin(s)' % len(core_plugins))
		for plugin_info in core_plugins:
			plugin = self._import_plugin(plugin_info)
			if not plugin:
				logging.error('Error: Failed to load core plugin. Offender: %s' % plugin_info['id'])
				continue
			else:
				logging.good('Loaded %s' % plugin_info['id'])

			plugins_loaded.append(plugin)
		logging.down()

		logging.info('Sorting dependencies')
		sorted_plugins = []
		while plugins_additional:
			added_any = False
			for plugin in plugins_additional:
				ready_add = True
				if 'dependencies' in plugin and 'plugin' in plugin['dependencies']:
					for dependency in plugin['dependencies']['plugin']:
						found = False
						for ready_plugin in sorted_plugins:
							if ready_plugin['id'] == dependency:
								found = True
								break
						if not found:
							ready_add = False
				if ready_add:
					added_any = True
					sorted_plugins.append(plugin)
					plugins_additional.remove(plugin)
			if not added_any:
				break
		plugins_additional = sorted_plugins

		logging.up('Loading %s additional plugin(s)' % len(plugins_additional))
		# Start plugins if they're set to be enabled.
		for plugin_info in plugins_additional:
			if not self.is_enabled(plugin_info['id']):
				self._insert_dummy_plugin(plugin_info)
				continue

			plugin = self._import_plugin(plugin_info)
			if not plugin:
				logging.error('Error: Failed to load additional plugin. Offender: %s' % plugin_id)
				continue
			else:
				logging.good('Loaded %s' % plugin_info['id'])

			plugins_loaded.append(plugin)
		logging.down()

		logging.up('Initializing %s plugin(s)' % len(plugins_loaded))
		for plugin in plugins_loaded:
			plugin._info['_is_enabled'] = True
			self._init_plugin(plugin)
		logging.down()

	def _import_plugin(self, plugin_info):
		module = __import__(plugin_info['_id'], globals(), locals())

		plugin = None
		module_views = list()

		for name, obj in self.get_classes(module, prism.api.plugin.BasePlugin):
			plugin = obj()
			plugin._module = module
			plugin._info = plugin_info
			plugin._endpoint = plugin.plugin_id

			self.plugins[plugin.plugin_id] = plugin
			# Break. Just in case they imported another plugin's base class
			break

		for name, obj in self.get_classes(module, api.view.BaseView):
			module_views.append(obj)

		if plugin is None:
			logging.error('Error: Invalid plugin in plugins folder. Offender: %s' % plugin_id)
			return False

		plugin._module_views = module_views
		return plugin

	def _init_plugin(self, plugin):
		"""
		Initializes a plugin:
			1. Runs the plugin's init() function.
			2. Saves the config
			3. Loads the plugin's endpoints into flask
		"""
		logging.up('Starting %s v%s' % (plugin.name, plugin.version))

		def get_wrapper(plugin):
			def func():
				return plugin
			return func
		setattr(plugin.__class__, 'get', get_wrapper(plugin))

		plugin.init(PRISM_STATE)
		plugin.config.save()

		blueprint_name = plugin._endpoint
		# Create the plugin blueprint in flask
		plugin._blueprint = Blueprint(blueprint_name,
										plugin._info['_id'],
										template_folder='templates')

		if len(plugin._module_views) > 0:
			self.possible_permissions[plugin.plugin_id] = {}

			# Go through each of the module's views and add them to flask
			for view_class in plugin._module_views:
				view = view_class()

				self.possible_permissions[plugin.plugin_id][str(view_class.__name__)] = view.title

				endpoint_id = '%s' % view_class.__name__

				with flask_app().app_context():
					if view.menu is not None:
						# Generate the parent menu item
						if 'parent' in view.menu:
							if '.' not in view.menu['parent']['id']:
								parts = ('/' + blueprint_name + view.endpoint).split('/')
								flask_app().add_url_rule('/'.join(parts[:-1]),
																endpoint=blueprint_name + '.' + view.menu['parent']['id'])
								item = current_menu.submenu(view.menu['parent']['id'])
								item.register(blueprint_name + '.' + view.menu['parent']['id'],
												view.menu['parent']['text'],
												view.menu['parent']['order'],
												icon=view.menu['parent']['icon'])
							else:
								item = current_menu.submenu(view.menu['parent']['id'])
								item.register(blueprint_name + '.' + endpoint_id,
												view.menu['parent']['text'],
												view.menu['parent']['order'],
												icon=view.menu['parent']['icon'])
						item = current_menu.submenu(view.menu['id'])
						item.register(blueprint_name + '.' + endpoint_id,
										view.title,
										view.menu['order'],
										icon=view.menu['icon'])
						if prism.settings.is_dev():
							logging.info('Registered menu item for /%s: %s' % (blueprint_name + view.endpoint, view.menu['id']))
					else:
						# Generate a hidden menu item so titles show correctly
						item = current_menu.submenu(generate_random_string(12))
						item.register(blueprint_name + '.' + endpoint_id,
										view.title,
										hidden=True)

				# Find all methods in the view class
				for func_name in [method for method in dir(view) if callable(getattr(view, method))]:
					if func_name.startswith('_'):
						continue

					if func_name not in ['get', 'post', 'put', 'delete']:
						if not func_name.endswith(('_get', '_post', '_put', '_delete')):
							continue
						else:
							# Set the fallback http method to the extention of the function name
							parts = func_name.split('_')
							if parts[len(parts) - 1] in ('get', 'post', 'put', 'delete'):
								fallback_http_methods = [parts[len(parts) - 1].upper()]
					else:
						# Set the fallback http method to the function name
						fallback_http_methods = [func_name.upper()]

					if func_name == 'get':
						endpoint_id = '%s' % view_class.__name__
					elif func_name.endswith('_get'):
						endpoint_id = '%s:%s' % (view_class.__name__, '_'.join(func_name.split('_')[:-1]))
					else:
						endpoint_id = '%s:%s' % (view_class.__name__, func_name)

					# Get the method
					func = getattr(view, func_name)
					view_func_wrapper = self.func_wrapper(plugin.plugin_id, func)

					# If the http methods have been specified in the @subroute decorator
					if hasattr(func, 'http_methods'):
						fallback_http_methods = func.http_methods

					fallback_endpoint = '/'
					# If an endpoint has been specified in the @subroute decorator
					if hasattr(func, 'endpoint'):
						fallback_endpoint = func.endpoint

					# Prepare fallback defaults for the page
					if hasattr(func, 'defaults'):
						fallback_defaults = func.defaults
					elif func.__defaults__ is not None:
						args, varargs, keywords, defaults = inspect.getargspec(func)
						fallback_defaults = dict(zip(args[-len(defaults):], defaults))
					else:
						fallback_defaults = {}

					func_routes = list()
					if not hasattr(func, 'routes'):
						func_routes.append({
												'endpoint': fallback_endpoint,
												'http_methods': fallback_http_methods,
												'defaults': fallback_defaults
											})
					else:
						func_routes = func.routes

					# Add a route for the get function with no parameters
					if func_name == 'get':
						plugin._blueprint.add_url_rule(view.endpoint + fallback_endpoint,
														endpoint=endpoint_id,
														methods=fallback_http_methods,
														view_func=view_func_wrapper,
														defaults=fallback_defaults)

					for route in func_routes:
						if 'endpoint' not in route:
							route['endpoint'] = fallback_endpoint
						if 'http_methods' not in route:
							route['http_methods'] = fallback_http_methods
						if 'defaults' not in route:
							route['defaults'] = fallback_defaults.copy()

						# Defaults are odd. They cannot be attached to routes with the key in the url
						# For example: if <id> in in the url rule, it cann't be in defaults.
						pattern = re.compile(r'<(?:.+?(?=:):)?(.+?)>')
						if '<' in route['endpoint'] and len(route['defaults']) > 0:
							for id in re.findall(pattern, route['endpoint']):
								try:
									del route['defaults'][id]
								except:
									pass

						if prism.settings.is_dev():
							logging.info('Registered page /%s: %s %s' % (blueprint_name + view.endpoint + route['endpoint'], blueprint_name + '.' + endpoint_id, route['http_methods']))

						plugin._blueprint.add_url_rule(view.endpoint + route['endpoint'],
														endpoint=endpoint_id,
														methods=route['http_methods'],
														view_func=view_func_wrapper,
														defaults=route['defaults'])

		flask_app().register_blueprint(plugin._blueprint, url_prefix='/' +
															blueprint_name.replace('.', '/'))

		plugin.post_init(PRISM_STATE)

		logging.down()

	def func_wrapper(self, plugin_id, func):
		""" Wraps the route return function. This allows us
		to do fun things with the return value :D """
		def func_wrapper(*args, **kwargs):
			return handle_render(plugin_id, func, flask.request, *args, **kwargs)
		func_wrapper.__name__ = func.__name__
		return func_wrapper

def handle_render(plugin_id, obj, *args, **kwargs):
	import prism.login
	check_perm = True
	if hasattr(obj.__self__.__class__, '_check_permissions'):
		check_perm = obj.__self__.__class__._check_permissions
	if check_perm:
		if not prism.login.user().has_permission(plugin_id + '.' + str(obj.__self__.__class__.__name__)):
			return flask.abort(403)

	hold_previous = flask.g.current_plugin
	flask.g.current_plugin = plugin_id
	obj = obj(*args, **kwargs)

	# from flask import request, redirect, url_for, render_template
	if isinstance(obj, tuple):
		page_args = {}
		if len(obj) > 1:
			page_args = obj[1]

		if obj[0].endswith('.html'):
			obj = flask.render_template(obj[0], **page_args)
		elif get_url_for(obj[0]) is not None:
			obj = flask.redirect(get_url_for(obj[0], **page_args))
		elif len(obj) > 1:
			if obj[0] == 'redirect':
				obj = flask.redirect(flask.url_for(obj[1]))
			elif obj[0] == 'abort':
				obj = flask.abort(obj[1])
			elif obj[0] == 'error':
				error_json = base64.b64encode(json.dumps(page_args).encode('utf-8'))
				obj = flask.redirect(flask.url_for('core.ErrorView', error_json=error_json))
	elif isinstance(obj, str):
		if obj.endswith('.html'):
			obj = flask.render_template(obj)
		elif get_url_for(obj) is not None:
			obj = flask.redirect(get_url_for(obj))
	elif isinstance(obj, dict):
		obj = flask.jsonify(obj)
	elif isinstance(obj, prism.api.view.View):
		obj = jinja2.Markup(obj.render())
	flask.g.current_plugin = hold_previous
	return obj

# Utility functions
def public_endpoint(function):
	""" Use as a decorator to allow a page to
	be accessed without being logged in """
	function.is_public = True
	return function

def get_url_for(url, **args):
	""" Checks if a url endpoint exists """
	try:
		return flask.url_for(url, **args)
	except:
		return None

def command(cmd):
	""" Runs a shell command and returns the output """
	return subprocess.call(cmd, shell=True)

def get_input(string, default=None, allow_empty=True):
	""" Gets input from the user in the shell """
	if default:
		string = string + '[' + default + ']'

	while True:
		user_input = input('\033[1m\033[33m??> %s: \033[0m' % string)
		if user_input or allow_empty:
			break
	if not user_input:
		return (default, True)
	return (user_input, False)

def get_password(string, default=None, allow_empty=True):
	import getpass
	""" Gets input from the user in the shell """
	if default:
		string = string + '[' + default + ']'

	while True:
		user_input = getpass.getpass('\033[1m\033[33m**> %s: \033[0m' % string)
		if user_input or allow_empty:
			break
	if not user_input:
		return (default, True)
	return (user_input, False)

def get_yesno(string):
	ret = None
	while ret is None:
		user_input = input('::| %s [y/n]: ' % string)
		if user_input == "yes" or user_input == "y":
			ret = True
		elif user_input == "no" or user_input == "n":
			ret = False
	return ret

def generate_random_string(length):
	""" Returns a string of random characters of size "length" """
	import random
	import string
	return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))

def is_package_installed(pkg):
	""" Returns true of the linux system has a
	binary installed under the name "pkg" """
	out, err = os_command('rpm -qa | grep %s' % pkg,
						'dpkg -l | grep %s' % pkg)
	return (len(out) > 0)

def get_os_command(redhat, debian=None):
	os = get_general_os()
	if os == 'redhat' or debian is None:
		return redhat
	elif os == 'debian':
		return debian

def os_command(redhat, debian=None):
	""" Runs a command based on the OS currently in use """
	process = subprocess.Popen(get_os_command(redhat, debian), shell=True, stdout=PIPE, stderr=PIPE)
	return process.communicate()

def os_commands(redhat, debian=None, scl=None, user=None):
	""" Runs a list of commands in a shell based on the OS currently in use """
	commands = get_os_command(redhat, debian)

	cmd_str = ''

	if user is not None:
		cmd_str += 'sudo su %s -c \'' % user

	if scl is not None and scl:
		cmd_str += 'scl enable %s "' % scl

	for cmd in commands:
		cmd_str += '%s;' % cmd

	if scl is not None and scl:
		cmd_str += '"'

	if user is not None:
		cmd_str += '\''

	process = subprocess.Popen(cmd_str, shell=True, stdout=PIPE, stderr=PIPE)
	return process.communicate()

# Returns if the OS is a Debian, Red Hat, or Ubuntu
def get_general_os():
	""" Gets a simple name of the current linux operating system """
	if any(word in platform.platform() for word in ("redhat", "centos", "fedora")):
		return 'redhat'
	else:
		return 'debian'

def data_merge(a, b):
	key = None
	try:
		if a is None or isinstance(a, str) or isinstance(a, int) or isinstance(a, float):
			# border case for first run or if a is a primitive
			a = b
		elif isinstance(a, list):
			# lists can be only appended
			if isinstance(b, list):
				# merge lists
				a.extend(b)
			else:
				# append to list
				a.append(b)
		elif isinstance(a, dict):
			# dicts must be merged
			if isinstance(b, dict):
				for key in b:
					if key in a:
						a[key] = data_merge(a[key], b[key])
					else:
						a[key] = b[key]
			else:
				raise Exception('Cannot merge non-dict "%s" into dict "%s"' % (b, a))
		else:
			raise Exception('NOT IMPLEMENTED "%s" into "%s"' % (b, a))
	except TypeError as e:
		raise Exception('TypeError "%s" in key "%s" when merging "%s" into "%s"' % (e, key, b, a))
	return a
