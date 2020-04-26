""" High-Level Synthesis Task-Level Parallelization.

See:
https://github.com/Blaok/tlp
"""

import re

from os import path

from setuptools import find_packages, setup

here = path.abspath(path.dirname(__file__))

with open(path.join(here, '..', '..', 'README.md'), encoding='utf-8') as f:
  long_description = f.read()

with open(path.join(here, '..', '..', 'CMakeLists.txt'), encoding='utf-8') as f:
  version = '.'.join(x[1] for x in re.finditer(
      r'set\(CPACK_PACKAGE_VERSION_..... (.*)\)', f.read()))

setup(
    name='tlpc',
    version=version,
    description='High-level synthesis task-level parallelization',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/Blaok/tlp',
    author='Blaok Chi',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Intended Audience :: Developers',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Topic :: System :: Hardware',
    ],
    packages=find_packages(),
    python_requires='>=3.6',
    install_requires=['haoda>=0.0.20200425.dev1', 'pyverilog>=1.2.0'],
    entry_points={
        'console_scripts': ['tlpc=tlp.tlpc:main', 'tlpv=tlp.tlpv:main'],
    },
    include_package_data=True,
)
