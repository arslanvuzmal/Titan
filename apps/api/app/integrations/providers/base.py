from abc import ABC


class IntegrationAdapter(ABC):
    """
    Base interface for all third-party integrations.
    Requires an access token to be instantiated (which should be decrypted by the vault).
    """

    def __init__(self, access_token: str):
        self.access_token = access_token
