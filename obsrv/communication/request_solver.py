import asyncio
import logging
from typing import List

from obsrv.communication.base_request_solver import BaseRequestSolver
from obcom.data_colection.address import AddressError
from obsrv.tree_components.base_components.tree_component import ProvidesResponseProtocol
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class RequestSolver(BaseRequestSolver):

    def __init__(self, data_provider: ProvidesResponseProtocol, **kwargs):
        super(RequestSolver, self).__init__(data_provider, **kwargs)

    async def get_answer(self, request: List[bytes], user_id: bytes, timeout=None) -> List[bytes]:
        # docstring is imported from parent
        response = []
        coroutines = []
        for r in request:
            coroutines.append(self.get_single_answer(r, user_id, timeout=timeout))
        result = await asyncio.gather(*coroutines, return_exceptions=True)
        for r in result:
            if isinstance(r, bytes):
                response.append(r)
            elif isinstance(r, BaseException):
                # This is a precaution against unexpected program failures. To run properly, all errors should be
                # caught in '_get_single_answer' method.
                logger.error(f"CRITICAL One of the sub-tasks raise some unresolved exception - {type(r)}: {r}.")
                re = ResponseError(4001, 'There were unexpected problems trying to respond to the request', repr(self),
                                   ResponseError.SEVERITY_CRITICAL)
                v_response = ValueResponse('', None, False, re)
                resp = v_response.to_byte()
                response.append(resp)
            else:
                logger.error(f"CRITICAL One of the sub-tasks return not supported type response - {type(r)}: {r}.")
                re = ResponseError(4001, 'There were unexpected problems trying to respond to the request', repr(self),
                                   ResponseError.SEVERITY_CRITICAL)
                v_response = ValueResponse('', None, False, re)
                resp = v_response.to_byte()
                response.append(resp)
        return response

    async def get_single_answer(self, request: bytes, user_id: bytes, timeout=None) -> bytes:
        # docstring is imported from parent
        response: bytes
        try:
            v_request = ValueRequest.from_byte(request)
        except (ValueError, AddressError, TypeError):
            # can not create ValueRequest object (data is damaged) - return empty response witch error
            logger.info('Can not convert request dictionary to request object.')
            re = ResponseError(4001, 'Can not build request from ordered data', repr(self),
                               ResponseError.SEVERITY_CRITICAL)
            v_response = ValueResponse('', None, False, re)
            response = v_response.to_byte()
            return response
        # set user ID
        v_request.user.socket_id = user_id
        # set timeout - this is only for make sure
        if v_request.request_timeout != timeout and timeout:
            # logger.warning('The timeout value passed in the request does not match the actually set. It will be set '
            #                'to real')
            v_request.request_timeout = timeout

        v_response = await self._get_single_answer(v_request=v_request)
        response = v_response.to_byte()
        return response
