from abc import (
    ABC,
    abstractmethod,
)
from contextlib import contextmanager

from nutkit.frontend import (
    Driver,
    FakeTime,
)
import nutkit.protocol as types
from tests.shared import driver_feature
from tests.stub.authorization.test_authorization import AuthorizationBase
from tests.stub.shared import StubServer


class _TestVerifyAuthenticationBase(AuthorizationBase, ABC):

    required_features = types.Feature.API_DRIVER_VERIFY_AUTHENTICATION,

    backwards_compatible_auth = None

    VERIFY_AUTH_NEGATIVE_ERRORS = (
        "Neo.ClientError.Security.CredentialsExpired",
        "Neo.ClientError.Security.Forbidden",
        "Neo.ClientError.Security.TokenExpired",
        "Neo.ClientError.Security.Unauthorized",
    )

    VERIFY_AUTH_PROPAGATE_ERRORS = (
        # Don't include AuthorizationExpired as it's explicitly handled to not
        # fail fast during discovery. Hence, it does not behave like other
        # security errors when returned from the router.
        # "Neo.ClientError.Security.AuthorizationExpired",
        "Neo.ClientError.Security.MadeUp",
        "Neo.ClientError.Security.AuthenticationRateLimit",
    )

    def setUp(self):
        super().setUp()
        self._router = StubServer(9000)
        self._reader = StubServer(9010)
        self._auth1 = types.AuthorizationToken("basic", principal="neo4j",
                                               credentials="pass")
        self._auth2 = types.AuthorizationToken("basic", principal="neo5j",
                                               credentials="pass++")

    def tearDown(self):
        self._router.reset()
        self._reader.reset()
        super().tearDown()

    def get_vars(self):
        return {
            "#HOST#": self._router.host,
            "#VERSION#": "5.1"
        }

    @contextmanager
    def driver(self, routing=False):
        auth = self._auth1
        if routing:
            uri = f"neo4j://{self._router.address}"
        else:
            uri = f"bolt://{self._reader.address}"
        driver = Driver(
            self._backend, uri, auth,
            backwards_compatible_auth=self.backwards_compatible_auth
        )
        try:
            yield driver
        finally:
            driver.close()

    def verify_authentication(self, driver):
        if self.session_auth:
            return driver.verify_authentication(self._auth2)
        else:
            return driver.verify_authentication()

    def start_server(self, server, script_fn, vars_=None):
        if self.session_auth:
            script_fn = f"session_auth_{script_fn}"
        else:
            script_fn = f"driver_auth_{script_fn}"
        super().start_server(server, script_fn, vars_)

    @property
    @abstractmethod
    def session_auth(self) -> bool:
        ...

    def _test_successful_authentication(self):
        def test(routing_, warm_):
            suffix = "_warm" if warm_ else ""
            if routing_:
                self.start_server(self._router, f"router{suffix}.script")
            self.start_server(self._reader, f"reader{suffix}.script")

            with self.driver(routing=routing_) as driver:
                if warm_:
                    session = driver.session("r", database="system")
                    list(session.run("RETURN 1"))
                    session.close()
                res = self.verify_authentication(driver)

            self.assertIs(res, True)

            if routing_:
                self._router.done()
            self._reader.done()

            if self.driver_supports_features(
                types.Feature.OPT_MINIMAL_VERIFY_AUTHENTICATION
            ):
                if routing_:
                    self.assertEqual(self._router.count_requests("LOGON"), 1)
                logon_count = self._reader.count_requests("LOGON")
                run_count = self._reader.count_requests("RUN")
                expected_logon_count = 2 if warm_ else 1
                expected_run_count = 1 if warm_ else 0
                self.assertEqual(logon_count, expected_logon_count)
                self.assertEqual(run_count, expected_run_count)

        for routing in (False, True):
            for warm in (False, True):
                with self.subTest(routing=routing, warm=warm):
                    test(routing, warm)
                self._router.reset()
                self._reader.reset()

    def _test_router_failure(self):
        # only works with cold routing driver

        def test(error_, raises_, router_script_):
            self.start_server(self._router, router_script_,
                              vars_={**self.get_vars(), "#ERROR#": error_})
            with self.driver(routing=True) as driver:
                if raises_:
                    with self.assertRaises(types.DriverError) as exc:
                        self.verify_authentication(driver)
                    self.assertEqual(exc.exception.code, error_)
                else:
                    res = self.verify_authentication(driver)
                    self.assertIs(res, False)
            self._router.done()

        for (error, raises) in (
            *((e, False) for e in self.VERIFY_AUTH_NEGATIVE_ERRORS),
            *((e, True) for e in self.VERIFY_AUTH_PROPAGATE_ERRORS),
        ):
            for router_script in ("router_error_logon.script",
                                  "router_error_route.script"):
                with self.subTest(error=error, raises=raises,
                                  script=router_script):
                    test(error, raises, router_script)
                self._router.done()
                self._router.reset()

    @driver_feature(types.Feature.BACKEND_MOCK_TIME)
    def _test_warm_router_failure(self):
        # only works with routing driver

        def test(error_, raises_, router_script_):
            with FakeTime(self._backend) as time:
                self.start_server(self._router, router_script_,
                                  vars_={**self.get_vars(), "#ERROR#": error_})
                self.start_server(self._reader, "reader_warm.script")
                with self.driver(routing=True) as driver:
                    # warm up driver
                    session = driver.session("r", database="system")
                    list(session.run("RETURN 1"))
                    session.close()
                    # make routing table expire
                    time.tick(1_001_000)
                    if raises_:
                        with self.assertRaises(types.DriverError) as exc:
                            self.verify_authentication(driver)
                        self.assertEqual(exc.exception.code, error_)
                    else:
                        res = self.verify_authentication(driver)
                        self.assertIs(res, False)
                self._router.done()

        router_scripts = ["router_error_route_warm.script"]
        if self.session_auth:
            router_scripts.append("router_error_logon_warm.script")
        for (error, raises) in (
            *((e, False) for e in self.VERIFY_AUTH_NEGATIVE_ERRORS),
            *((e, True) for e in self.VERIFY_AUTH_PROPAGATE_ERRORS),
        ):
            for router_script in router_scripts:
                with self.subTest(error=error, raises=raises,
                                  script=router_script):
                    test(error, raises, router_script)
                self._router.reset()
                self._reader.reset()

    def _test_reader_failure(self):
        def test(routing_, warm_, error_, raises_):
            suffix = "_warm" if warm_ else ""
            if routing_:
                self.start_server(self._router, f"router{suffix}.script")
            self.start_server(self._reader, f"reader_error{suffix}.script",
                              vars_={**self.get_vars(), "#ERROR#": error_})
            with self.driver(routing=routing_) as driver:
                if warm_:
                    session = driver.session("r", database="system")
                    list(session.run("RETURN 1"))
                    session.close()
                if raises_:
                    with self.assertRaises(types.DriverError) as exc:
                        self.verify_authentication(driver)
                    self.assertEqual(exc.exception.code, error_)
                else:
                    res = self.verify_authentication(driver)
                    self.assertIs(res, False)
            if routing_:
                self._router.done()
            self._reader.done()

        for (error, raises) in (
            *((e, False) for e in self.VERIFY_AUTH_NEGATIVE_ERRORS),
            *((e, True) for e in self.VERIFY_AUTH_PROPAGATE_ERRORS),
            ("Neo.ClientError.Security.AuthorizationExpired", True),
        ):
            for routing in (False, True):
                for warm in (False, True):
                    with self.subTest(routing=routing, warm=warm, error=error,
                                      raises=raises):
                        test(routing, warm, error, raises)
                    self._router.reset()
                    self._reader.reset()


class TestVerifyAuthenticationDriverAuthV5x1(_TestVerifyAuthenticationBase):

    required_features = (*_TestVerifyAuthenticationBase.required_features,
                         types.Feature.BOLT_5_1)

    session_auth = False

    def test_successful_authentication(self):
        super()._test_successful_authentication()

    def test_router_failure(self):
        super()._test_router_failure()

    def test_warm_router_failure(self):
        super()._test_warm_router_failure()

    def test_reader_failure(self):
        super()._test_reader_failure()


class TestVerifyAuthenticationSessionAuthV5x1(_TestVerifyAuthenticationBase):

    required_features = (*_TestVerifyAuthenticationBase.required_features,
                         types.Feature.BOLT_5_1)

    session_auth = True

    def test_successful_authentication(self):
        super()._test_successful_authentication()

    def test_router_failure(self):
        super()._test_router_failure()

    def test_warm_router_failure(self):
        super()._test_warm_router_failure()

    def test_reader_failure(self):
        super()._test_reader_failure()


class TestVerifyAuthenticationV5x0(_TestVerifyAuthenticationBase):

    required_features = (*_TestVerifyAuthenticationBase.required_features,
                         types.Feature.BOLT_5_0)

    session_auth = False

    def get_vars(self):
        return {
            **super().get_vars(),
            "#VERSION#": "5.0"
        }

    def test_is_not_supported(self):
        def test(routing_, warm_):
            if routing_:
                self.start_server(self._router, "router.script")
            self.start_server(self._reader, "reader.script")

            with self.driver(routing=routing_) as driver:
                if warm_:
                    session = driver.session("r", database="system")
                    list(session.run("RETURN 1"))
                    session.close()
                with self.assertRaises(types.DriverError) as exc:
                    self.verify_authentication(driver)
                self.assert_re_auth_unsupported_error(exc.exception)

            self._router.reset()
            self._reader.reset()

        for routing in (False, True):
            for warm in (False, True):
                with self.subTest(routing=routing, warm=warm):
                    test(routing, warm)
                self._router.reset()
                self._reader.reset()


class _BackwardsCompatibilityBase(_TestVerifyAuthenticationBase, ABC):

    required_features = (*_TestVerifyAuthenticationBase.required_features,
                         types.Feature.BOLT_5_0)

    backwards_compatible_auth = True

    def get_vars(self):
        return {
            **super().get_vars(),
            "#VERSION#": "5.0"
        }

    def start_server(self, server, script_fn, vars_=None):
        parts = script_fn.split(".")
        script_fn = f"{parts[0]}_backwards_compat.{parts[1]}"
        super().start_server(server, script_fn, vars_)



class TestVerifyAuthenticationDriverAuthV5x0BackwardsCompatibility(
    _BackwardsCompatibilityBase
):

    session_auth = False

    def test_successful_authentication(self):
        super()._test_successful_authentication()

    def test_router_failure(self):
        super()._test_router_failure()

    def test_warm_router_failure(self):
        super()._test_warm_router_failure()

    def test_reader_failure(self):
        super()._test_reader_failure()


class TestVerifyAuthenticationSessionAuthV5x0BackwardsCompatibility(
    _BackwardsCompatibilityBase
):

    session_auth = True

    def test_successful_authentication(self):
        super()._test_successful_authentication()

    def test_router_failure(self):
        super()._test_router_failure()

    def test_warm_router_failure(self):
        super()._test_warm_router_failure()

    def test_reader_failure(self):
        super()._test_reader_failure()
# TODO v5x0 with and without backwards compatibility config
