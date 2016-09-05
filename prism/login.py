from passlib.hash import sha256_crypt
import flask
from flask import Blueprint, request, redirect, url_for, render_template

import flask_login
from wtforms import Form, StringField, PasswordField, validators

import prism


flask_app = prism.flask_app()
db = prism.get().database

login_manager = flask_login.LoginManager()
login_manager.init_app(flask_app)

class User(db.Model):
	__tablename__ = 'user'

	username = db.Column(db.String, primary_key=True)
	password = db.Column(db.String)
	permissions = db.Column(db.String)

	def __init__(self, username, password, permissions):
		self.username = username
		self.password = password
		self.permissions = permissions

	def get_id(self):
		return self.username

	def is_authenticated(self):
		return True

	def is_active(self):
		return True

	def is_anonymous(self):
		return False

	def __repr__(self):
		return '<User %r>' % self.username

class LoginForm(Form):
	username = StringField('Username', validators=[validators.Required()])
	password = PasswordField('Password', validators=[validators.Required()])

def create_user(username, password, permissions=[]):
	db.session.add(User(username, sha256_crypt.encrypt(password), ','.join(permissions)))
	db.session.commit()

def user():
	""" Returns the user object if they're logged in, otherwise None """
	from flask import g
	if hasattr(g, 'user'):
		return g.user
	return None

def is_logged_in():
	""" Returns true if the user is logged in """
	from flask import g
	if hasattr(g, 'user'):
		return g.user is not None and g.user.is_authenticated
	return False

@flask_app.route("/", methods=['GET', 'POST'])
@prism.public_endpoint
def login():
	if is_logged_in():
		return redirect(url_for('dashboard.DashboardView'))

	form = LoginForm(request.form)
	if request.method == 'POST' and form.validate():
		user = User.query.get(form.username.data)
		if user:
			if sha256_crypt.verify(form.password.data, user.password):
				flask_login.login_user(user, remember=True)
				return redirect(url_for('dashboard.DashboardView'))
		flask.flash('Sorry, that username/password combination was incorrect.')
	return render_template('other/login.html', title='Login', form=form)

@flask_app.before_request
def revalidate_login():
	flask.g.current_plugin = None
	flask.g.user = flask_login.current_user

	if (flask.request.endpoint and
		not flask.request.endpoint.startswith('static') and
		not getattr(flask_app.view_functions[flask.request.endpoint], 'is_public', False) and
		not is_logged_in()):
		return flask.redirect(flask.url_for('login'))

@flask_app.route('/logout')
def logout():
	flask_login.logout_user()
	return flask.redirect(flask.url_for('login'))

@login_manager.user_loader
def load_user(username):
	return User.query.get(username)

@login_manager.unauthorized_handler
def unauthorized_handler():
	""" When an unauthorized user attempts to go to a user-only page, send them back to the login page """
	return redirect(url_for('login.login'))
