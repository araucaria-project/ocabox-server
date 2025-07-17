class DeviceResponseError(Exception):
    pass


class PilarError(DeviceResponseError):

    def __init__(self, error_number: int, error_message: str):
        """Inicjalizuje obiekt PilarError."""
        self.error_number = error_number
        self.message = error_message
        super().__init__(f"Error {self.error_number}: {self.message}")

    def __str__(self):
        """Zwraca sformatowaną wiadomość błędu."""
        return f"Error {self.error_number}: {self.message}"


class RequestConnectionError(IOError):
    pass
