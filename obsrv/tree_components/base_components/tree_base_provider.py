import logging
from abc import ABC

from obcom.data_colection.address import AddressError
from obsrv.tree_components.base_components.tree_component import TreeComponent, ProvidesResponseProtocol
from obcom.data_colection.coded_error import TreeStructureError, TreeOtherError
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value import Value, TreeValueError
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeBaseProvider(TreeComponent, ABC):
    """
    P0

    :param component_name: this is name of tree component, used for debug
    :param subcontractor: instance of next component in tree

    :ivar _subcontractor: instance of next component in tree

    Conforms to:
        ProvidesResponseProtocol
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeBaseProvider'

    def __init__(self, component_name: str, subcontractor: ProvidesResponseProtocol = None, **kwargs):
        super().__init__(component_name=component_name, **kwargs)
        self._subcontractor: ProvidesResponseProtocol = subcontractor

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        """
        This is the main method of generating a return value from a response.
        A method can raise an TreeStructureError error if the address is not directed to that component but to components
        deeper in the tree. The method can also raise TreeValueError, AddressError errors if you encounter any problems
        generating the response. The errors may contain the appropriate error codes. This will send an error response
        to the client.

        :param request: ValueRequest
        :raise TreeStructureError: when method can't provide value and algorythm should looking a value in sub provider
        :raise TreeValueError: when the value cannot be generated and an error response should be returned to the client
        :raise AddressError: when address is wrong or damaged
        :raise TreeOtherError: when is some other errors
        :return: Value
        """
        raise TreeStructureError

    async def _on_subcontractor_return(self, result: ValueResponse, request: ValueRequest):
        """
        This method is run after receiving a response from the subcontractor. It can be used to decorate a reply or
        delete temporary data.

        :param result: generated response returned to the client
        :return: None
        """
        pass

    async def get_response(self, request: ValueRequest) -> ValueResponse:
        """
        This is the main method that will be called to get a response to a given request. It requires a defined
        get_value() method to work properly. If there are any problems reading the address, an empty response will be
        returned.

        :param request: ValueRequest
        :return: ValueResponse
        """
        try:
            v = await self.get_value(request)
            if isinstance(v, Value) or v is None:
                return ValueResponse(request.address, v, True)
            logger.error(f'Method get_value() returned wrong type, expected Value or None')
            re = ResponseError(2002, 'Can not create value', repr(self), severity=ResponseError.SEVERITY_NORMAL)
            return ValueResponse(request.address, None, False, re)
        except TreeStructureError:
            if self._subcontractor:
                try:
                    result = await self._subcontractor.get_response(request)
                    await self._on_subcontractor_return(result, request)
                    return result
                except AttributeError:
                    re = ResponseError(3002, '', repr(self), ResponseError.SEVERITY_CRITICAL)
                    logger.warning(f"Provider try call subcontractor but he doesn't has method get_response()")
                    return ValueResponse(request.address, None, False, re)
            else:
                # This provider doesn't provide response and there is no next provider
                re = ResponseError(3001, '', repr(self), ResponseError.SEVERITY_CRITICAL)
                logger.info(f'Provider try call subcontractor but he does not exist')
                return ValueResponse(request.address, None, False, re)
        except TreeValueError as e:
            re = ResponseError.from_coded_error(component_name=repr(self), err=e)
            return ValueResponse(request.address, None, False, re)
        except AddressError as e:
            re = ResponseError.from_coded_error(component_name=repr(self), err=e)
            return ValueResponse(request.address, None, False, re)
        except TreeOtherError as e:
            re = ResponseError.from_coded_error(component_name=repr(self), err=e)

            return ValueResponse(request.address, None, False, re)

    async def run(self):
        # run self
        if self._subcontractor:
            await self._subcontractor.run()

    async def stop(self):
        # stop self
        if self._subcontractor:
            await self._subcontractor.stop()

    def post_init_tree(self, tree_data, tree_path: str):
        self.set_tree_path(address_to_object=tree_path)
        self._tree_data = tree_data
        if self._subcontractor:
            self._subcontractor.post_init_tree(tree_data=tree_data, tree_path=self.tree_path)

    def get_configuration(self) -> dict:
        out = self._get_self_configuration()
        if self._subcontractor:
            out.get(self.get_name()).get("child").update(self._subcontractor.get_configuration())
        return out
