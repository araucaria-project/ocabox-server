import aiohttp


class DeviceResponseError(Exception):
    pass


class AlpacaError(DeviceResponseError):
    """
    Exception for when Alpaca throws an error with a numeric value.

    :param error_number: Non-zero integer.
    :param error_message: Message describing the issue that was encountered.

    """

    def __init__(self, error_number: int, error_message: str):
        """Initialize NumericError object."""
        super().__init__(self)
        self.message = "Error %d: %s" % (error_number, error_message)

    def __str__(self):
        """Message to display with error."""
        return self.message


class AlpacaHttpError(DeviceResponseError):
    """
    Exception for when Alpaca throws an error without a numeric value.

    :param error_message: Message describing the issue that was encountered.

    """

    def __init__(self, error_message: str):
        """Initialize ErrorMessage object."""
        super().__init__(self)
        self.message = error_message

    def __str__(self):
        """Message to display with error."""
        return self.message


class AlpacaContentTypeError(aiohttp.ContentTypeError):
    """
    Exception for when Alpaca return data in wrong format.
    """


class AlpacaHttp400Error(AlpacaHttpError):
    pass


class AlpacaHttp500Error(AlpacaHttpError):
    pass


class RequestConnectionError(IOError):
    pass
