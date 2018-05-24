# foobar-api

[![Travis CI](https://travis-ci.org/uppsaladatavetare/foobar-api.svg)](https://travis-ci.org/uppsaladatavetare/foobar-api)
[![Codecov](https://codecov.io/gh/uppsaladatavetare/foobar-api/coverage.svg?branch=master)](https://codecov.io/gh/uppsaladatavetare/foobar-api/)

This repository contains the backend for the FooBar kiosk and inventory system.

## Requirements

- Python 3.4+
- Django 1.10+
- virtualenv (recommended)
- [pdftotext](https://linux.die.net/man/1/pdftotext) for delivery report parsing

## Setup

    $ git clone git@github.com:uppsaladatavetare/foobar-api.git
    $ cd foobar-api
    $ virtualenv -p /usr/local/bin/python3.4 venv
    $ venv/bin/pip install -r requirements.txt

## If you want to try running a local version and never worked on a django project before
To try it locally you should edit the requirements.txt and replace "production" into "local"

Follow the setup guide stated above, note that your path to python can differ.
you can find out your path in linux with the whereis command

    $ whereis python3
    >> /usr/bin/python3.6
 

To be able to open the site locally you have to allow localhost to the admin-page,
add this into src/foobar/settings/local.py:

    $ ALLOWED_HOSTS += ['localhost', '127.0.0.1']



Enter your virtual environment where all dependency-programs have been installed

    $ source venv/bin/activate

before you are able to enter, you have to migrate the database for the first time as well as add a superuser:

    $ cd src/
    $ python manage.py migrate
    $ python manage.py createsuperuser

Lastly, start your server. You will be able to log in to http://127.0.0.1:8000/admin/ (note the "/admin/" part, foobar-api does not have a homepage) with your superuser account.

Remember to always activate your virtual environment before if you start the server at a later time.

    $ python manage.py runserver


## How do I run tests?

We use [tox](https://tox.readthedocs.org/en/latest/) to automate testing against all supported Python and Django versions. To run test, simply execute following command in the root directory:

    $ tox

## Can I contribute?

Sure thing! Any contributions are welcome.

## License

MIT License. Please see the LICENSE file.
