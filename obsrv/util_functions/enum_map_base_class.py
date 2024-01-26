class BaseArgsMap:

    @classmethod
    def has_item(cls, item):
        """
        Method checks if class has property by name.

        :param item: value of item
        :return:
        """
        return item in [v for i, v in cls.__dict__.items() if i[:1] != '_']

    @classmethod
    def get_item_by_name(cls, item):
        """
        Attention !!!. A very specific method only useful with `StrVal` objects. The method takes an element by
        value and returns its value.

        :param item: value of item
        :return:
        """
        r = None
        for i, v in cls.__dict__.items():
            if i[:1] != '_' and item == v:
                r = v
        return r


class StrVal(str, BaseArgsMap):
    _VAL_ = None

    def __new__(cls, string, v):
        instance = super().__new__(cls, string)
        instance._VAL_ = v
        return instance

    def val(self):
        return self._VAL_


