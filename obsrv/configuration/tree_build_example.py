from obsrv.comunication.request_solver import RequestSolver
from obsrv.comunication.router import Router
from obsrv.data_colection.base_components.tree_base_broker import TreeBaseBroker
from obsrv.data_colection.base_components.tree_base_broker_default_target import TreeBaseBrokerDefaultTarget
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_blocker_access_grantor import TreeBlockerAccessGrantor
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_cctv import TreeCCTV
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.data_colection.specialistic_components.tree_base_request_blocker import TreeBaseRequestBlocker
from obsrv.data_colection.specialistic_components.tree_ephemeris import TreeEphemeris
from obsrv.data_colection.specialistic_components.tree_plan_executor import TreePlanExecutor
from obsrv.ob_config import SingletonConfig


def tree_build() -> Router:
    # load custom config file
    SingletonConfig.add_config_file_from_config_dir('sample_config.yaml')
    SingletonConfig.get_config(rebuild=True).get()  # this method refreshes the configuration with newly added files,
    # it must be called once after adding all files
    # tree
    # --------------------------------------- global ---------------------------------------
    ephemeris = TreeEphemeris('ephemeris', 'ephemeris')
    broker_components_global = TreeBaseBrokerDefaultTarget('broker-components-global',
                                                           [ephemeris],
                                                           default_provider=None)
    cache_global = TreeCache('cache-global', broker_components_global)
    conditional_freezer_global = TreeConditionalFreezer('conditional-freezer-global', cache_global)
    target_provider_global = TreeProvider('target-provider-global', 'global', conditional_freezer_global)

    # --------------------------------------- zb08 ---------------------------------------
    alpaca_zb08 = TreeAlpacaObservatory('alpaca-zb08', observatory_name='zibi-ocabox-800')
    alpaca_blocker_zb08 = TreeBaseRequestBlocker('alpaca-blocker-zb08', alpaca_zb08)
    blocker_grantor_zb08 = TreeBlockerAccessGrantor('access-grantor-zb08', 'access_grantor', alpaca_blocker_zb08)
    plan_executor_zb08 = TreePlanExecutor('executor-zb08', 'executor')
    cctv_zb08 = TreeCCTV('cctv-zb08', 'cctv')
    # ephemeris_zb08 = TreeEphemeris('ephemeris-zb08', 'ephemeris')
    broker_components_zb08 = TreeBaseBrokerDefaultTarget('broker-components-zb08',
                                                         [blocker_grantor_zb08, plan_executor_zb08, cctv_zb08],
                                                         default_provider=alpaca_blocker_zb08)
    cache_zb08 = TreeCache('cache-zb08', broker_components_zb08)
    conditional_freezer_zb08 = TreeConditionalFreezer('conditional-freezer-zb08', cache_zb08)
    target_provider_zb08 = TreeProvider('target-provider-zb08', 'zb08', conditional_freezer_zb08)

    # ---------
    alpaca_module = TreeAlpacaObservatory('zibi_sample', observatory_name='zibi-ocabox-800')
    alpaca_blocker = TreeBaseRequestBlocker('zibi_blocker', alpaca_module)
    blocker_grantor = TreeBlockerAccessGrantor('grantor_800', 'access_grantor', alpaca_blocker)
    broker_800 = TreeBaseBrokerDefaultTarget('broker_800', [blocker_grantor], default_provider=alpaca_blocker)
    cache_800 = TreeCache('sample_cache', broker_800)
    conditional_freezer_800 = TreeConditionalFreezer('conditional_freezer_800', cache_800)

    tp_800 = TreeProvider('module_800', '800', conditional_freezer_800)
    vb = TreeBaseBroker('front_broker', [tp_800, target_provider_global, target_provider_zb08])

    # front receiver blocks
    rs = RequestSolver(vb)
    vr = Router(rs)
    return vr
