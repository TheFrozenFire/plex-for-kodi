# coding=utf-8
"""A slow plex.tv sign-in must not stick the add-on on the previous user."""
from __future__ import absolute_import

import ast
import json
import os
import time

import requests
import xbmcgui

from lib import hubrefresh, remotenav
from plexnet import http

from .base import KodiTestCase, REPO_ROOT


_SIGN_IN_OK = (
    '<user id="2" title="User B" username="user-b" email="" '
    'thumb="https://example.invalid/t" authenticationToken="token-new" '
    'restricted="0" secure="1"><profile_settings/></user>'
)


class _Body(object):
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.ok = 200 <= status < 300
        self.content = text.encode("utf-8")
        self.headers = {}
        self.reason = "OK"

    def close(self):
        return None


class _Manager(object):
    def publish(self):
        return None

    def refreshResources(self, force=False):
        return None


class _Servers(object):
    reachabilityNeverTested = False


class SwitchValidationTest(KodiTestCase):
    """The switch POST succeeded. The follow-up sign-in is what fails."""

    def setUp(self):
        super(SwitchValidationTest, self).setUp()
        from .base import ensure_plex_interface
        ensure_plex_interface()
        from plexnet import myplexaccount, plexapp
        self.plexapp = plexapp
        self.http = http
        self.myplexrequest = __import__("plexnet.myplexrequest", fromlist=["MyPlexRequest"])
        self._orig = (
            plexapp.ACCOUNT, plexapp.util.ACCOUNT, plexapp.util.LOCAL_MODE,
            plexapp.util.MANAGER, plexapp.SERVERMANAGER, plexapp.refreshResources,
            http.HttpRequest.startAsync,
            self.myplexrequest.MyPlexRequest.postToStringWithTimeout,
        )
        plexapp.refreshResources = lambda force=False: None
        plexapp.util.MANAGER = _Manager()
        plexapp.SERVERMANAGER = _Servers()
        plexapp.util.LOCAL_MODE = False
        hubrefresh.reset()
        hubrefresh.note_script_thread()
        remotenav._reset_for_tests()

        account = myplexaccount.MyPlexAccount()
        account.ID = "1"
        account.title = "User A"
        account.username = "user-a"
        account.authToken = "token-old"
        account.isSignedIn = True
        account.isAuthenticated = True
        account.isOffline = False
        account.isProtected = False
        account.lastHomeUserUpdate = time.time()
        account.homeUsers = [
            myplexaccount.HomeUser({"id": "1", "title": "User A", "protected": "0"}),
            myplexaccount.HomeUser({"id": "2", "title": "User B", "protected": "0"}),
        ]
        for user in account.homeUsers:
            user.isAdmin = user.id == "1"
            user.isManaged = False
            user.isProtected = False
        account.fetchServerTokens = lambda token: {}
        account.refreshSubscription = lambda: None
        plexapp.ACCOUNT = account
        plexapp.util.ACCOUNT = account
        self.account = account
        self.outcomes = []
        self.timeouts = []
        self._install_transport()

    def tearDown(self):
        (self.plexapp.ACCOUNT, self.plexapp.util.ACCOUNT, self.plexapp.util.LOCAL_MODE,
         self.plexapp.util.MANAGER, self.plexapp.SERVERMANAGER, self.plexapp.refreshResources,
         self.http.HttpRequest.startAsync,
         self.myplexrequest.MyPlexRequest.postToStringWithTimeout) = self._orig
        # A switch imports the server manager, which then listens for security
        # changes. Point the globals at that existing object. Do not import it
        # here: constructing it reads the account, which has just been restored.
        if self.plexapp.util.MANAGER is None:
            self.plexapp.util.MANAGER = _Manager()
        if self.plexapp.SERVERMANAGER is None:
            import sys
            manager = sys.modules.get("plexnet.plexservermanager")
            if manager is not None:
                self.plexapp.SERVERMANAGER = manager.MANAGER
        _stop_player_monitor()
        remotenav._reset_for_tests()
        hubrefresh.reset()
        super(SwitchValidationTest, self).tearDown()

    def _install_transport(self):
        outcomes = self.outcomes
        timeouts = self.timeouts

        def start_async(request, body=None, contentType=None, context=None):
            timeouts.append(float(context.timeout))
            item = outcomes.pop(0)
            if isinstance(item, BaseException):
                context.failure = http.failure_kind(item)
                request.onResponse(None, context)
            else:
                context.failure = None
                request.onResponse(item, context)
            return True

        def post_switch(request, body=None, timeout=None, return_on_status_code=False):
            return b'<user authenticationToken="token-new"/>'

        self.http.HttpRequest.startAsync = start_async
        self.myplexrequest.MyPlexRequest.postToStringWithTimeout = post_switch

    def test_a_validation_timeout_keeps_the_old_user_and_the_picker_can_open(self):
        self.outcomes.extend([
            requests.exceptions.ReadTimeout(),
            requests.exceptions.ReadTimeout(),
        ])
        result = remotenav.accept_notification('{"to":"user","id":"2"}')

        self.assertEqual("timeout", result["reason"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["offline"])
        self.assertEqual("1", self.account.ID)
        self.assertEqual("User A", self.account.title)
        self.assertEqual("token-old", self.account.authToken)
        self.assertFalse(self.account.isOffline)
        self.assertTrue(self.account.isSignedIn)
        self.assertGreaterEqual(self.timeouts[1], 10.0)
        self.assertGreater(self.timeouts[1], self.timeouts[0])

        # Switch User clears authentication and then asks whether to open the picker.
        self.account.isAuthenticated = False
        from lib.main import userPickerAllowed
        self.assertTrue(userPickerAllowed(self.account, local_mode=False))

        stored = json.loads(xbmcgui.Window(10000).getProperty("script.plex.userswitch"))
        self.assertEqual("timeout", stored["reason"])
        status = json.loads(xbmcgui.Window(10000).getProperty("script.plex.userstatus"))
        self.assertEqual("1", status["id"])
        self.assertFalse(status["offline"])

    def test_a_retry_adopts_the_new_user(self):
        self.outcomes.extend([
            requests.exceptions.ReadTimeout(),
            _Body(_SIGN_IN_OK),
        ])
        result = remotenav.accept_notification('{"to":"user","id":"2"}')

        self.assertTrue(result["ok"])
        self.assertEqual("switched", result["reason"])
        self.assertEqual("2", self.account.ID)
        self.assertEqual("User B", self.account.title)
        self.assertEqual("token-new", self.account.authToken)
        self.assertFalse(self.account.isOffline)
        self.assertIsNone(self.account.switchError)
        status = json.loads(xbmcgui.Window(10000).getProperty("script.plex.userstatus"))
        self.assertEqual("2", status["id"])
        self.assertEqual("User B", status["name"])
        self.assertFalse(status["offline"])

    def test_unreachable_is_not_reported_as_a_timeout(self):
        self.outcomes.extend([
            requests.exceptions.ConnectionError(),
            requests.exceptions.ConnectionError(),
        ])
        result = remotenav.accept_notification('{"to":"user","id":"2"}')

        self.assertEqual("unreachable", result["reason"])
        self.assertFalse(result["ok"])
        self.assertEqual("1", self.account.ID)
        self.assertEqual("token-old", self.account.authToken)
        self.assertFalse(self.account.isOffline)
        self.assertEqual("unreachable", self.account.switchError)

    def test_a_read_timeout_and_a_connection_error_are_different_kinds(self):
        self.assertEqual("timeout", http.failure_kind(requests.exceptions.ReadTimeout()))
        self.assertEqual("unreachable", http.failure_kind(requests.exceptions.ConnectionError()))

    def test_a_connection_error_still_finishes_an_account_request(self):
        seen = []
        context = http.RequestContext()
        context.requestType = "sign_in"
        context.callback = lambda event, ctx: seen.append(ctx.failure)
        self.assertTrue(http._complete_account_failure(context, "unreachable"))
        self.assertEqual(["unreachable"], seen)

        other = http.RequestContext()
        other.requestType = "library"
        other.callback = lambda event, ctx: seen.append("library")
        self.assertFalse(http._complete_account_failure(other, "unreachable"))
        self.assertEqual(["unreachable"], seen)


class UserStatusStartupTest(KodiTestCase):
    def setUp(self):
        super(UserStatusStartupTest, self).setUp()
        from .base import ensure_plex_interface
        ensure_plex_interface()
        from plexnet import myplexaccount, plexapp
        self.plexapp = plexapp
        self._orig = (plexapp.ACCOUNT, plexapp.util.ACCOUNT)
        account = myplexaccount.MyPlexAccount()
        account.ID = "1"
        account.title = "User A"
        account.isSignedIn = True
        account.isOffline = False
        plexapp.ACCOUNT = account
        plexapp.util.ACCOUNT = account
        self.account = account
        remotenav._reset_for_tests()

    def tearDown(self):
        remotenav._reset_for_tests()
        self.plexapp.ACCOUNT, self.plexapp.util.ACCOUNT = self._orig
        super(UserStatusStartupTest, self).tearDown()

    def test_status_is_published_at_startup(self):
        published = remotenav.arm_user_status()
        self.assertEqual("1", published["id"])
        self.assertEqual("User A", published["name"])
        self.assertFalse(published["offline"])
        self.assertTrue(published["signedIn"])
        stored = json.loads(xbmcgui.Window(10000).getProperty("script.plex.userstatus"))
        self.assertEqual(published, stored)

        self.account.isOffline = True
        self.plexapp.util.APP.trigger("account:response")
        updated = json.loads(xbmcgui.Window(10000).getProperty("script.plex.userstatus"))
        self.assertTrue(updated["offline"])
        self.assertEqual("1", updated["id"])

    def test_startup_and_home_open_publish_status(self):
        main = ast.parse(_source("lib", "main.py"), filename="lib/main.py")
        home = ast.parse(_source("lib", "windows", "home.py"), filename="lib/windows/home.py")
        self.assertIn("arm_user_status", _calls(main, "_main"))
        self.assertIn("publish_user_status", _calls(home, "displayServerAndUser"))
        self.assertIn("userstatus", _source("docs", "remote-play.md"))


def _stop_player_monitor():
    """Importing main starts the player monitor, and the stub wait does not sleep."""
    import sys
    player = sys.modules.get("lib.player")
    if player is None:
        return
    player.PLAYER.close()
    thread = getattr(player.PLAYER, "thread", None)
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


def _source(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _calls(tree, function_name):
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            found = node
            break
    names = []
    for child in ast.walk(found):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            names.append(child.func.attr)
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            names.append(child.func.id)
    return names
