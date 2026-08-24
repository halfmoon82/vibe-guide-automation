from setuptools import find_packages, setup


setup(
    name="vibe-guide",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.9",
    entry_points={"console_scripts": ["vibe=vibe_guide.cli:main"]},
)
