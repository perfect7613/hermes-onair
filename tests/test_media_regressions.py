"""Regressions for replacing generated media and rebuilding existing scenes."""
import os
from pathlib import Path

from .test_app import AppCase


class MediaRegressionTests(AppCase):
    def media(self, name, data=b'media'):
        path = self.dir / name
        path.write_bytes(data)
        return str(path)

    def test_same_size_same_timestamp_replacement_is_imported(self):
        first = self.media('first.wav', b'first')
        second = self.media('second.wav', b'other')
        os.utime(first, (1700000000, 1700000000))
        os.utime(second, (1700000000, 1700000000))
        self.app.stage.play(first)
        result = self.app.stage.play(second)
        self.assertEqual(Path(result['file']).read_bytes(), b'other')

    def test_similar_scene_names_keep_independent_media(self):
        first = self.app.design('Intro!', background=self.media('first.png', b'first'))
        second = self.app.design('Intro?', background=self.media('second.png', b'other'))
        self.assertNotEqual(first['file'], second['file'])
        self.assertEqual(Path(first['file']).read_bytes(), b'first')

    def test_switching_browser_and_media_backgrounds_replaces_old_picture(self):
        self.app.design('Intro', html='<body>Old picture</body>')
        self.app.design('Intro', background=self.media('first.png'), title='Old title')
        self.assertNotIn('On Air Scene · Intro', [x['sourceName'] for x in self.obs.scenes['Intro']])
        self.app.design('Intro', html='<body>New picture</body>')
        names = [x['sourceName'] for x in self.obs.scenes['Intro']]
        self.assertNotIn('On Air Image · Intro', names)
        self.assertNotIn('On Air Title · Intro', names)

    def test_rebuilding_without_title_removes_old_title(self):
        path = self.media('first.png')
        self.app.design('Intro', background=path, title='Old title')
        self.app.design('Intro', background=path)
        self.assertNotIn('On Air Title · Intro', [x['sourceName'] for x in self.obs.scenes['Intro']])

    def test_play_rejects_remote_video_instead_of_silently_making_sticker(self):
        result = self.tool('live_overlay', action='play', file='https://example.com/clip.mp4')
        self.assertIn('web link', result.get('error', ''))
        self.assertEqual(self.obs.browser_events, [])

    def test_restyling_an_existing_scene_while_live_requires_approval(self):
        self.obs.streaming = True
        self.assertIsNotNone(self.app.needs_approval('live_design_scene', {'name': 'Main', 'background': '/tmp/x.mp4'}))

    def test_media_rebuild_keeps_background_below_existing_sources(self):
        self.app.design('Intro', background=self.media('first.gif'), title='Title')
        self.app.source('add', 'Custom label', scene='Intro', kind='text', text='Keep me')
        self.app.design('Intro', background=self.media('second.mp4'), title='Title')
        self.assertEqual(self.obs.scenes['Intro'][0]['sourceName'], 'On Air Video · Intro')
        self.assertIn('Custom label', [x['sourceName'] for x in self.obs.scenes['Intro']])

    def test_played_sticker_has_a_persistent_copy(self):
        from urllib.parse import unquote, urlsplit
        self.app.setup()
        original = self.media('sticker.gif')
        self.tool('live_overlay', action='play', file=original)
        url = self.overlay_messages()[-1]['url']
        Path(original).unlink()
        self.assertEqual(Path(unquote(urlsplit(url).path)).read_bytes(), b'media')

    def test_similar_scene_names_keep_independent_html(self):
        first = self.app.design('Intro!', html='<body>first</body>')
        second = self.app.design('Intro?', html='<body>second</body>')
        self.assertNotEqual(first['file'], second['file'])
        self.assertEqual(Path(first['file']).read_text(), '<body>first</body>')
