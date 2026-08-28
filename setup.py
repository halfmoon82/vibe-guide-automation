from setuptools import find_packages, setup


setup(
    name="vibe-guide",
    version="0.1.0",
    description="Installable Vibe Coding development guide CLI",
    packages=find_packages(),
    python_requires=">=3.9",
    entry_points={"console_scripts": ["vibe=vibe_guide.cli:main"]},
    include_package_data=True,
)

