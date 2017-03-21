from .hbase_client import HBaseClient


class ProfileFetcher:
    """ Fetch the latest information for a client on HBase.
    """
    def __init__(self):
        self.hbase_client = HBaseClient()

    def get(self, client_id):
        profile_data = self.hbase_client.get_client_profile(client_id)
        addon_ids = [addon['addon_id']
                     for addon in profile_data['active_addons']]
        return {
            "installed_addons": addon_ids,
            "locale": profile_data['locale']
        }
