import toml


class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self


try:
    config = toml.load('config.toml', AttrDict)
except FileNotFoundError:
    print("Please rename 'config.example.toml' to 'config.toml' and change the relevant values")
