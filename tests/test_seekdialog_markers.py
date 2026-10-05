# coding=utf-8
"""Production marker selection/countdown uses landed time, not seek notification targets."""
from __future__ import absolute_import

from types import SimpleNamespace
from unittest.mock import patch

from kodienv import ENV
ENV.abort_requested = True
from lib import player
from lib.windows import seekdialog
from tests.base import KodiTestCase


class RecordingDialog(seekdialog.SeekDialog):
    def __init__(self):
        p = SimpleNamespace(isExternal=False, playerObject=SimpleNamespace(startOffset=0),
                            playState='playing', STATE_PLAYING='playing', STATE_PAUSED='paused', position=1490.689)
        p.getTime = lambda: p.position
        self.handler = SimpleNamespace(player=p, seekOnStart=None, waitingForSOS=False,
                                       seekBackTo=None, playlist=None, creditMarkerHit=None)
        self.props = {}
        self.initialized = True
        self._ignoreTick = False
        self.offset = 1492040
        self.selectedOffset = self.offset
        self.pausedAt = None
        self.ldTimer = False
        self.idleTime = None
        self.hasDialog = False
        self._playlistDialogVisible = False
        self._seeking = self._applyingSeek = False
        self.autoSeekTimeout = None
        self.timeout = float('inf')
        self.useAlternateSeek = False
        self.isDirectPlay = True
        self.baseOffset = 0
        self._duration = 1600000
        self._enableMarkerSkip = True
        self.showSkipIntro = self.showSkipCredits = True
        self.showIntroSkipEarly = False
        self.bingeMode = False
        self.autoSkipCredits = True
        self._creditsSkipShownStarted = None
        self.skipCreditsButtonTimeout = 100
        self._currentMarker = None
        self._navigatedViaMarkerOrChapter = False
        self.lastFocusID = None
        self.osd = False
        self.seeks = []
        self.updated = []
        self.resets = 0
        m = seekdialog.MARKERS['credits'].copy()
        m.update(marker_type='credits', marker=seekdialog.Marker({
            'startTimeOffset':1495040, 'endTimeOffset':1600000, 'final':True}))
        self._markers = [m]

    def setProperty(self, name, value):
        self.props[name] = value

    def getProperty(self, name):
        return self.props.get(name, '')

    def getBoolProperty(self, name):
        return bool(self.getProperty(name))

    def osdVisible(self):
        return self.osd

    def updateCurrent(self, **kwargs):
        self.updated.append(self.offset)

    def resetSeeking(self):
        self.resets += 1

    def resetAutoSeekTimer(self, value):
        self.autoSeekTimeout = None

    def doSeek(self, *args, **kwargs):
        self.seeks.append(self.offset)


class SeekDialogMarkerTest(KodiTestCase):
    def setUp(self):
        KodiTestCase.setUp(self)
        self.setting = patch.object(seekdialog.util.addonSettings, 'autoSkipOffset', 1)
        self.setting.start()
        self.addCleanup(self.setting.stop)
        self.d = RecordingDialog()

    def test_optimistic_chapter_target_does_not_flash_then_hide_countdown(self):
        visible = []
        names = []
        # Device E1: requested early-show boundary1492040, actual playback is behind it.
        for position in (1490.689, 1491.809, 1492.889, 1494.089, 1495.089):
            self.d.player.position = position
            self.d.tick()
            visible.append(bool(self.d.getProperty('show.markerSkip')))
            names.append(self.d.getProperty('skipMarkerName'))
        self.assertEqual(visible, [False, False, True, True, True])
        self.assertEqual(names[2:], ['Skipping credits (3)', 'Skipping credits (2)', 'Skipping credits (1)'])
        self.assertEqual(self.d.seeks, [], 'keyframe preroll must not cause a corrective seek')

    def test_backward_seek_hides_marker_on_first_actual_position_tick(self):
        self.d.player.position = 1493
        self.d.tick()
        self.assertTrue(self.d.getProperty('show.markerSkip'))
        self.d.player.position = 1400
        self.d.tick()
        self.assertFalse(self.d.getProperty('show.markerSkip'))
        self.assertIsNone(self.d._markers[0]['countdown'])

    def test_current_position_crossing_marker_boundary_is_not_one_tick_late(self):
        self.d.offset = 1491000
        self.d.player.position = 1493
        self.d.tick()
        self.assertTrue(self.d.getProperty('show.markerSkip'))

    def test_buffer_wait_refreshes_position_after_wait(self):
        self.d.offset = 1491000
        def buffered():
            self.d.player.position = 1493
            return True
        self.d.waitForBuffer = buffered
        self.d.tick(waitForBuffer=True)
        self.assertTrue(self.d.getProperty('show.markerSkip'))
        self.assertEqual(self.d.offset, 1493000)

    def test_cancelled_buffer_wait_does_not_refresh_or_advance_markers(self):
        self.d.waitForBuffer = lambda: False
        self.d.tick(waitForBuffer=True)
        self.assertEqual(self.d.offset, 1492040)
        self.assertIsNone(self.d._currentMarker)

    def test_stopped_player_resets_without_advancing_marker_countdown(self):
        def stopped():
            raise RuntimeError('Stopped')
        self.d.player.getTime = stopped
        self.d.tick()
        self.assertEqual(self.d.resets, 1)
        self.assertIsNone(self.d._currentMarker)

    def test_startup_seek_and_explicit_preinit_offset_keep_their_existing_path(self):
        for field in ('seekOnStart', 'waitingForSOS', 'seekBackTo'):
            with self.subTest(field=field):
                d = RecordingDialog()
                d.player.position = 1493
                setattr(d.handler, field, 5000)
                d.tick()
                self.assertIsNone(d._currentMarker)
        self.d.initialized = False
        self.d.tick(offset=5000)
        self.assertEqual(self.d.seeks, [5000])
        self.assertIsNone(self.d._currentMarker)

    def test_osd_pauses_countdown_and_cancel_hides_it_without_seek(self):
        self.d.player.position = 1493
        self.d.tick()
        count = self.d._markers[0]['countdown']
        self.d.osd = True
        self.d.tick()
        self.assertEqual(self.d._markers[0]['countdown'], count)
        self.d.displayMarkers(cancelTimer=True)
        self.assertFalse(self.d.getProperty('show.markerSkip'))
        self.assertEqual(self.d.seeks, [])
