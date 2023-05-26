from xmlrpc.client import ServerProxy


class SuseManager:
    def __init__(self, url, username, password):
        self.url = url
        self.username = username
        self.password = password
        self.client = None
        self.token = None

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.logout()

    def login(self):
        if self.client is None:
            self.client = ServerProxy('{}/rpc/api'.format(self.url), use_datetime=True)
            self.token = self.client.auth.login(self.username, self.password)

    def logout(self):
        if self.client is not None:
            self.client.auth.logout(self.token)
            self.client = None
            self.token = None

    def exec(self, class_name, function_name, args=None, retries=0):
        if self.client is None:
            return None
        args = [] if args is None else args
        function_obj = getattr(getattr(self.client, class_name), function_name)
        return function_obj(self.token, *args)

    def get_device_id(self, device_name):
        return self.exec('system', 'getId', (device_name,))[0]['id']

    def get_package_ids(self, device_id, package_name, package_version):
        packages = []
        for package in self.exec('system', 'listAllInstallablePackages', (device_id,)):
            if package['name'] == package_name and package['version'] == package_version:
                packages.append(package['id'])
        return packages

    def get_installed_package_ids(self, device_id, package_name):
        packages = []
        for package in self.exec('system', 'listInstalledPackages', (device_id,)):
            if package['name'] == package_name:
                packages.append(package['package_id'])
        return packages
