from setuptools import setup

setup(
    name='llmeshowyou',
    version='1.0.0',
    py_modules=['llmeshowyou', 'llmeshowyou_gui'],
    entry_points={
        'console_scripts': [
            'llmeshowyou=llmeshowyou:main',
        ],
    },
    python_requires='>=3.10',
    description='Language-aware file-to-markdown mapper for LLM context efficiency',
    url='https://github.com/aserajian1-gif/llmeshowyou',
)
