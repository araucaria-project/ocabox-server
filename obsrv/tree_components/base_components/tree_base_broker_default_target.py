from typing import List

from obcom.data_colection.address import AddressError
from obsrv.tree_components.base_components.tree_base_broker import TreeBaseBroker
from obsrv.tree_components.base_components.tree_component import AddressedResponderProtocol, ProvidesResponseProtocol
from obcom.data_colection.value_call import ValueRequest


class TreeBaseBrokerDefaultTarget(TreeBaseBroker):
    """
    This is a base broker witch default provider. This component behaves the same as base broker except that when a
    request contains an address to a nonexistent provider, the request is routed to the default provider instead of
    returning an invalid address message. The default provider does not have to be on the list of all providers.

    :ivar _list_providers: list all next providers in tree
    :ivar _default_provider: default provider

    :param component_name: this is name of tree component, used for debug
    :param list_providers: list all next providers in tree
    :param default_provider: provider witch is chosen when broker can not find right provider

    Conforms to:
        AddressedResponderProtocol
    """

    def __init__(self, component_name: str, list_providers: List[AddressedResponderProtocol] = None,
                 default_provider: ProvidesResponseProtocol = None, **kwargs):
        super().__init__(component_name=component_name, **kwargs)
        self._list_providers: List[AddressedResponderProtocol] = list_providers \
            if list_providers and isinstance(list_providers, list) else []
        self._default_provider: ProvidesResponseProtocol = default_provider

    def set_default_provider(self, provider: ProvidesResponseProtocol):
        """Method sets default provider for this component"""
        self._default_provider = provider

    def remove_default_provider(self):
        """Method remove default provider"""
        self._default_provider = None

    def get_default_provider(self) -> ProvidesResponseProtocol:
        """Method return current default provider"""
        return self._default_provider

    def _get_provider(self, v_req: ValueRequest) -> AddressedResponderProtocol:
        """
        This method return provider for give address if is known else return default provider.
        If request has incorrect address this method raise AddressError

        :param v_req: ValueRequest
        :raise AddressError: if is wrong format address
        :return: return provider if exist or None if not
        """
        try:
            provider = super(TreeBaseBrokerDefaultTarget, self)._get_provider(v_req=v_req)
        except AddressError:
            # If there is no address pointing to the next block, it takes the default one
            provider = None
        if not provider:
            provider = self._default_provider
        return provider

    def get_all_providers(self) -> list:
        prov = self.get_list_providers()
        if self.get_default_provider() in prov or self.get_default_provider() is None:
            return prov
        else:
            return [*prov, self.get_default_provider()]

    async def run(self):
        # run self
        await super().run()
        if self._default_provider:
            await self._default_provider.run()

    async def stop(self):
        # stop self
        await super().stop()
        if self._default_provider:
            await self._default_provider.stop()

    def post_init_tree(self, tree_data, tree_path: str):
        super().post_init_tree(tree_data=tree_data, tree_path=tree_path)
        if self._default_provider:
            self._default_provider.post_init_tree(tree_data=tree_data, tree_path=self.tree_path)

    def get_resources(self):
        providers = self.get_list_providers()
        out = []
        for p in providers:
            r = p.get_resources()
            out.extend(r)
        out.extend(self._default_provider.get_resources())
        return out

    def get_configuration(self) -> dict:
        out = self._get_self_configuration()
        for p in self.get_all_providers():
            out.get(self.get_name()).get("child").update(p.get_configuration())
        return out
