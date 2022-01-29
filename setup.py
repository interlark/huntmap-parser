from setuptools import setup
import os

here = os.path.dirname(os.path.realpath(__file__))
with open(os.path.join(here, 'requirements.txt')) as f:
    install_requires = f.read().splitlines()

setup(
    name='huntmap-parser',
    author_email='interlark@gmail.com',
    version='0.1',
    install_requires=install_requires
)

