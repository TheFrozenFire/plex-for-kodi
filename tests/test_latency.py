# coding=utf-8
"""The first latency cuts, each behind a setting that defaults on."""
from __future__ import absolute_import

from kodienv import ENV

from lib.windows import pagination

from .base import KodiTestCase


class _Page(list):
    def __init__(self, count, total):
        super(_Page, self).__init__([object()] * count)
        self.totalSize = total


class _Hub(pagination.BaseRelatedPaginator):
    thumbFallback = None

    def __init__(self, *args, **kwargs):
        self.asked = []
        super(_Hub, self).__init__(*args, **kwargs)

    def getData(self, offset, amount):
        self.asked.append((offset, amount))
        return _Page(min(amount, 3), 20)

    def createListItem(self, rel):
        class Item(object):
            def setProperty(self, *args, **kwargs):
                return None

            def setBoolProperty(self, *args, **kwargs):
                return None

        return Item()

    def prepareListItem(self, data, mli):
        return None


class _Control(object):
    def replaceItems(self, items):
        self.items = items

    def selectItem(self, index):
        return None

    def getSelectedItem(self):
        return None


class RelatedCountTest(KodiTestCase):
    def test_default_does_not_ask_for_a_count(self):
        class Item(object):
            @property
            def relatedCount(self):
                raise AssertionError("count-only request")

        self.assertIsNone(pagination.related_leaf_count(Item()))

    def test_setting_off_reads_the_count(self):
        ENV.settings["defer_related_count"] = "false"

        class Item(object):
            relatedCount = 7

        self.assertEqual(pagination.related_leaf_count(Item()), 7)

    def test_first_page_learns_the_total_without_a_count_request(self):
        hub = _Hub(_Control(), object(), leaf_count=None)
        hub.paginate()
        self.assertEqual(hub.asked, [(0, hub.initialPageSize)])
        self.assertEqual(hub.leafCount, 20)
