# coding=utf-8
"""Passout protection resets on real activity and still stops unattended autoplay.

The two-hour guard used to start when the player window opened, and only a key
on the post-play screen moved it. A pause, a seek, or a remote key during
playback left it alone, so a long session reached post-play with the guard
already expired and the next episode never started. Startup seeks, buffer
pauses, and content-rule seeks are not a person, and neither is Action(noop).
"""
from __future__ import absolute_import

import time

import xbmcgui
from kodienv import ENV, LOGINFO

ENV.abort_requested = True
from lib import player  # noqa: E402
from lib import util  # noqa: E402
from lib.windows import kodigui, seekdialog, videoplayer  # noqa: E402

from .base import KodiTestCase  # noqa: E402


class Action(object):
    """xbmcgui.Action compares equal to its id."""

    def __init__(self, action_id):
        self.action_id = action_id

    def __eq__(self, other):
        return self.action_id == other

    def getId(self):
        return self.action_id

    def getButtonCode(self):
        return 0


class _Player(object):
    def __init__(self):
        self.notes = 0
        self.commands = []

    def noteWatchActivity(self):
        self.notes += 1

    def control(self, cmd, force=False):
        self.commands.append(cmd)


class _Handler(object):
    def __init__(self):
        self.player = _Player()
        self.seekOnStart = None
        self.seekingBackTo = False
        self.waitingForSOS = False
        self.pausedForSeek = False
        self.passoutArmed = True
        self.passoutSuppressSeek = False
        self.passoutSuppressTransport = 0
        self.seekBackTo = None
        self.seekBackToDone = True


class _Duration(object):
    def __init__(self, ms):
        self.ms = ms

    def asInt(self):
        return self.ms


class _QuietThread(object):
    def __init__(self, target=None, args=(), kwargs=None):
        self.target = target

    def start(self):
        return None


def _dialog(handler=None, ignore_input=True):
    handler = handler or _Handler()
    dlg = seekdialog.SeekDialog.__new__(seekdialog.SeekDialog)
    dlg.handler = handler
    dlg._hideDelay = 4
    dlg._lastAction = None
    dlg._ignoreInput = ignore_input
    dlg.idleTime = None
    dlg.useAlternateSeek = False
    dlg.videoPausedForAudioStreamChange = False
    dlg.resumeSeekBehindPause = False
    dlg.resumeSeekBehind = 0
    dlg.resumeSeekBehindAfter = 0
    dlg.ldTimer = False
    dlg.pausedAt = None
    return dlg


def _window():
    win = videoplayer.VideoPlayerWindow.__new__(videoplayer.VideoPlayerWindow)
    win.postPlayMode = False
    win.passoutProtection = 0
    win.timeout = None
    win.next = object()
    win.prev = type("Prev", (), {"duration": _Duration(0)})()
    win.windowId = 13001
    win.sessionID = "session"
    return win


class PassoutActivityTest(KodiTestCase):
    def setUp(self):
        super(PassoutActivityTest, self).setUp()
        ENV.abort_requested = True
        self._standby = util.MONITOR.tv_standby
        util.MONITOR.tv_standby = False
        self._debug = util.addonSettings.debug
        util.addonSettings.debug = False
        self._dialog_action = kodigui.BaseDialog.onAction
        kodigui.BaseDialog.onAction = lambda *args, **kwargs: None
        self._window_action = getattr(xbmcgui.WindowXML, "onAction", None)
        xbmcgui.WindowXML.onAction = lambda *args, **kwargs: None
        self._thread = videoplayer.threading.Thread
        videoplayer.threading.Thread = _QuietThread

    def tearDown(self):
        videoplayer.threading.Thread = self._thread
        kodigui.BaseDialog.onAction = self._dialog_action
        if self._window_action is None:
            del xbmcgui.WindowXML.onAction
        else:
            xbmcgui.WindowXML.onAction = self._window_action
        util.addonSettings.debug = self._debug
        util.MONITOR.tv_standby = self._standby
        player.PLAYER.off("user.activity", None)
        super(PassoutActivityTest, self).tearDown()

    def test_real_actions_count_and_noop_does_not(self):
        self.assertTrue(player.action_counts_as_watch_activity(Action(xbmcgui.ACTION_PAUSE)))
        self.assertTrue(player.action_counts_as_watch_activity(Action(xbmcgui.ACTION_PLAYER_PLAY)))
        self.assertTrue(player.action_counts_as_watch_activity(Action(xbmcgui.ACTION_PLAYER_PLAYPAUSE)))
        self.assertTrue(player.action_counts_as_watch_activity(Action(xbmcgui.ACTION_MOVE_LEFT)))
        self.assertTrue(player.action_counts_as_watch_activity(Action(xbmcgui.ACTION_STEP_FORWARD)))
        self.assertTrue(player.action_counts_as_watch_activity(Action(xbmcgui.ACTION_SELECT_ITEM)))
        self.assertFalse(player.action_counts_as_watch_activity(Action(0)))
        self.assertFalse(player.action_counts_as_watch_activity(Action(999)))
        self.assertFalse(player.action_counts_as_watch_activity(object()))

    def test_playback_and_post_play_keys_reset_the_deadline(self):
        win = _window()
        win.onAction(Action(xbmcgui.ACTION_PAUSE))
        self.assertGreater(win.passoutProtection, time.time() + 7000)

        frozen = win.passoutProtection = 50
        win.onAction(Action(0))
        win.onAction(Action(999))
        self.assertEqual(win.passoutProtection, frozen)

        win.postPlayMode = True
        win.timeout = time.time() + 10
        win.onAction(Action(xbmcgui.ACTION_MOVE_RIGHT))
        self.assertIsNone(win.timeout)
        self.assertGreater(win.passoutProtection, time.time() + 7000)

    def test_osd_keys_pause_resume_and_seek_reset_through_the_player(self):
        win = _window()
        win.resetPassoutProtection()
        opened = win.passoutProtection
        handler = _Handler()
        handler.player = player.PLAYER
        player.PLAYER.on("user.activity", win.onWatchActivity)
        dlg = _dialog(handler)
        try:
            handler.seekOnStart = 8000
            handler.passoutArmed = False
            dlg.onPlayBackSeek(8, 0)
            dlg.onAction(Action(999))
            self.assertEqual(win.passoutProtection, opened)

            win.passoutProtection = time.time() - 5
            dlg.onAction(Action(xbmcgui.ACTION_PAUSE))
            self.assertGreater(win.passoutProtection, time.time() + 7000)

            win.passoutProtection = time.time() - 5
            handler.passoutArmed = True
            dlg.onPlayBackPaused()
            self.assertGreater(win.passoutProtection, time.time() + 7000)

            win.passoutProtection = time.time() - 5
            dlg.onPlayBackResumed()
            self.assertGreater(win.passoutProtection, time.time() + 7000)

            win.passoutProtection = time.time() - 5
            handler.seekOnStart = None
            dlg.onPlayBackSeek(40, 0)
            self.assertGreater(win.passoutProtection, time.time() + 7000)
        finally:
            player.PLAYER.off("user.activity", win.onWatchActivity)

    def test_automatic_seeks_and_transport_do_not_count(self):
        handler = _Handler()
        dlg = _dialog(handler)

        handler.passoutArmed = False
        dlg.onPlayBackSeek(1, 0)
        dlg.onPlayBackPaused()
        dlg.onPlayBackResumed()
        self.assertEqual(handler.player.notes, 0)

        handler.passoutArmed = True
        handler.seekOnStart = 5000
        dlg.onPlayBackSeek(5, 0)
        handler.seekOnStart = None
        handler.seekingBackTo = True
        dlg.onPlayBackSeek(5, 0)
        handler.seekingBackTo = False
        handler.waitingForSOS = True
        dlg.onPlayBackSeek(5, 0)
        handler.waitingForSOS = False
        handler.pausedForSeek = True
        dlg.onPlayBackSeek(5, 0)
        self.assertEqual(handler.player.notes, 0)

        handler.pausedForSeek = True
        dlg.onPlayBackResumed()
        self.assertFalse(handler.pausedForSeek)
        self.assertEqual(handler.player.notes, 0)

        handler.seekBackTo = 50
        handler.seekBackToDone = False
        dlg.onPlayBackPaused()
        self.assertEqual(handler.player.notes, 0)

        handler.seekBackTo = None
        dlg.useAlternateSeek = True
        dlg.videoPausedForAudioStreamChange = True
        dlg.onPlayBackPaused()
        self.assertEqual(handler.player.commands, ["play"])
        self.assertEqual(handler.player.notes, 0)

        handler.passoutSuppressTransport = 2
        dlg.useAlternateSeek = False
        dlg.onPlayBackPaused()
        dlg.onPlayBackResumed()
        self.assertEqual(handler.passoutSuppressTransport, 0)
        self.assertEqual(handler.player.notes, 0)

        handler.passoutSuppressSeek = True
        dlg.onPlayBackSeek(12, 0)
        self.assertFalse(handler.passoutSuppressSeek)
        self.assertEqual(handler.player.notes, 0)

        dlg._ignoreInput = False
        dlg.onClick(1)
        dlg.onAction(Action(xbmcgui.ACTION_MOVE_LEFT))
        self.assertEqual(handler.player.notes, 2)

    def test_skip_rule_seek_is_not_a_person(self):
        handler = _Handler()
        host = player.PlexPlayer.__new__(player.PlexPlayer)
        host.handler = handler
        sought = []

        def seek_time(seconds):
            sought.append(seconds)

        host.seekTime = seek_time
        player.PlexPlayer._apply_skip_decision(host, {"action": "seek", "to_ms": 15000})
        self.assertEqual(sought, [15.0])
        self.assertTrue(handler.passoutSuppressSeek)

        dlg = _dialog(handler)
        dlg.onPlayBackSeek(15, 0)
        self.assertEqual(handler.player.notes, 0)
        self.assertFalse(handler.passoutSuppressSeek)

        def fail(seconds):
            raise RuntimeError("seek failed")

        host.seekTime = fail
        player.PlexPlayer._apply_skip_decision(host, {"action": "seek", "to_ms": 2000})
        self.assertFalse(handler.passoutSuppressSeek)

    def test_playlist_remote_key_counts(self):
        handler = _Handler()
        dlg = seekdialog.PlaylistDialog.__new__(seekdialog.PlaylistDialog)
        dlg.handler = handler
        dlg.onAction(Action(xbmcgui.ACTION_PAUSE))
        dlg.onAction(Action(999))
        self.assertEqual(handler.player.notes, 1)

    def test_unattended_autoplay_skips_and_logs_without_debug(self):
        win = _window()
        win.prev.duration = _Duration(videoplayer.PASSOUT_LAST_VIDEO_DURATION_MILLIS + 1)
        win.passoutProtection = time.time() - 1
        win.startTimer()
        self.assertIsNone(win.timeout)
        self.assertTrue(ENV.logged("Post play auto-play skipped: Passout protection", level=LOGINFO))

        # A short last episode never arms the guard, even after two quiet hours.
        ENV.log_lines[:] = []
        win.prev.duration = _Duration(videoplayer.PASSOUT_LAST_VIDEO_DURATION_MILLIS)
        win.startTimer()
        self.assertIsNotNone(win.timeout)
        self.assertFalse(ENV.logged("skipped: Passout protection"))

        # Activity during playback gives the next post-play a fresh two hours.
        ENV.log_lines[:] = []
        win.timeout = None
        win.prev.duration = _Duration(videoplayer.PASSOUT_LAST_VIDEO_DURATION_MILLIS + 1)
        win.onWatchActivity()
        win.startTimer()
        self.assertIsNotNone(win.timeout)
        self.assertGreater(win.passoutProtection, time.time() + 7000)
        self.assertFalse(ENV.logged("skipped: Passout protection"))
