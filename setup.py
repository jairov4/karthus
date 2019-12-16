import os

import setuptools

with open("README.md") as fp:
    long_description = fp.read()


def open_local(filename: str):
    return open(os.path.join(os.path.dirname(__file__), filename))


def read_requirements(filenames):
    """Utility function to read pip requirements files"""
    return [module for filename in filenames for module in list(open_local(filename).readlines())]


setuptools.setup(
    name="karthus",
    version="0.0.1",
    description="Fully automated Docker Swarm deployment",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Jairo Velasco",
    package_dir={"": "karthus"},
    packages=setuptools.find_packages(where="karthus"),
    install_requires=read_requirements(["requirements.txt"]),
    python_requires=">=3.6",
    classifiers=[
        "Development Status :: 4 - Beta",

        "Intended Audience :: Developers",

        "License :: OSI Approved :: Apache Software License",

        "Programming Language :: JavaScript",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",

        "Topic :: Software Development :: Code Generators",
        "Topic :: Utilities",

        "Typing :: Typed",
    ],
)
