from setuptools import find_packages, setup


setup(
    name="vibe-guide",
    version="3.10.0",
    packages=find_packages(),
    package_data={"vibe_guide.adapters": ["manifests/*.yaml"]},
    include_package_data=True,
    python_requires=">=3.9",
    entry_points={"console_scripts": ["vibe=vibe_guide.cli:main"]},
)
