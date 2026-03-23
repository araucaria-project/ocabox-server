import logging
from abc import ABC
from typing import Protocol, runtime_checkable, List

logger = logging.getLogger(__name__.rsplit('.')[-1])


class AddressDispatcher(ABC):
    """
    This class represents one component in the tree. All components except first must have a name.
    First component is initial so his name are not included in address

    :ivar _source_name: This is a component name with will be used to decode address
    :ivar auxiliary_source_names: list of auxiliary component names for address decoding.

    :param source_name: This is a component name with will be used to decode address
    :param auxiliary_source_names: list of auxiliary component names for address decoding. Thanks to this variable,
        it is possible to set more than one address name for a component

    Conforms:
        AddressedProtocol
    """

    def __init__(self, source_name: str, auxiliary_source_names: List[str] = None, **kwargs):
        super().__init__(**kwargs)
        self._source_name: str = 'default'  # this is to remember that there is such a variable
        if auxiliary_source_names is None:
            auxiliary_source_names = []
        self._set_source_name(source_name)  # here variable self._source_name is changed

        self._source_names = auxiliary_source_names
        if self._source_name not in self._source_names:
            self._source_names.append(self._source_name)

    def _set_source_name(self, source_name: str):
        """
        This method sets the name of this class to use when decoding the query address

        :param source_name: name for that class
        :raise ValueError: if source_name has wrong format or is None
        :return: None
        """
        if not source_name:
            logger.warning(f"Name of the component is None, so will be set to default name.")
            source_name = self._source_name
        if self._source_name == source_name:
            logger.warning(f"Name of the component is default, make sure you haven't forgotten to add name.")
        if not source_name or type(source_name) != str:
            raise ValueError
        self._source_name = source_name

    def get_source_name(self) -> str:
        """
        This method return name of this component to use when decoding the query address.

        :return: name of that component with will be used to decode address
        """
        return self._source_name

    def get_source_names(self) -> list:
        """
        This method return list name and auxiliary names of this component to use when decoding the query address.

        :return: list name and auxiliary names of that component with will be used to decode address
        """
        return self._source_names

    def is_named(self, name: str, only_main_name=False) -> bool:
        """
        This method check if this component is called by given name.

        :param name: name to check
        :param only_main_name: use only main source name without auxiliary source names
        :return: true if this component is called by given name or false if not
        """
        if only_main_name:
            if name == self.get_source_name():
                return True
            return False
        if name in self.get_source_names():
            return True
        return False

    def compare_source_names(self, list_names: List[str]) -> bool:
        """
        Method compare giving list of names and looking for match witch source names of this component.

        :param list_names: list of names
        :return: true if some value of giving list is on list names of this component
        """
        for name in list_names:
            if name in self.get_source_names():
                return True
        return False


@runtime_checkable  # this is need only for pycharm check correct code
class AddressedProtocol(Protocol):
    def get_source_name(self) -> str:
        """
        This method return name of this class to use when decoding the query address.

        :return: name of that component with will be used to decode address
        """
        pass

    def get_source_names(self) -> list:
        """
        This method return list names witch this class can consume when decoding the query address.

        :return: list names witch this class can consume when decoding the query address
        """
        pass

    def is_named(self, name: str, only_main_name=False) -> bool:
        """
        This method check if this component is called by given name.

        :param name: name to check
        :param only_main_name: use only main source name without auxiliary source names
        :return: true if this component is called by given name or false if not
        """
        pass

    def compare_source_names(self, list_names: List[str]) -> bool:
        """
        Method compare giving list of names and looking for match witch source names of this component.

        :param list_names: list of names
        :return: true if some value of giving list is on list names of this component
        """
        pass