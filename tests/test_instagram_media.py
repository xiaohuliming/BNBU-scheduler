"""Exercise the real yt-dlp product parser with synthetic, network-free posts."""
import copy
import unittest
from unittest import mock

from yt_dlp.extractor.instagram import InstagramIE
from yt_dlp.utils import DownloadError
from media_dl import ytdlp
from media_dl.transport import MediaDownloadError, headers_for

POST_URL = 'https://www.instagram.com/p/Dc2rki4PPPu/'


def photo(code, width=1600, height=2000):
    return {'pk': '1234', 'code': code, 'media_type': 1,
            'original_width': width, 'original_height': height,
            'image_versions2': {'candidates': [
                {'url': f'https://scontent.cdninstagram.com/{code}.jpg?signature=keep-me'},
                {'url': f'https://scontent.cdninstagram.com/{code}-small.jpg?signature=keep-me'},
            ]}}


def video(code):
    return {'pk': '1235', 'code': code, 'media_type': 2, 'has_audio': True,
            'video_versions': [{'url': f'https://scontent.cdninstagram.com/{code}.mp4',
                                'width': 1080, 'height': 1920}],
            'image_versions2': {'candidates': [{'url': 'https://scontent.cdninstagram.com/cover.jpg'}]}}


class InstagramMediaTests(unittest.TestCase):
    def extract(self, product):
        def network_fixture(ie, url):
            result = ie._extract_product(copy.deepcopy(product), video_id='Dc2rki4PPPu', get_comments=False)
            # Preserve the official single-post guard as well as YoutubeDL's
            # downstream format validation that originally rejected photos.
            if result.get('_type') != 'playlist' and not result.get('formats'):
                ie.raise_no_formats('There is no video in this post', expected=True)
            return result
        with mock.patch.object(InstagramIE, '_real_initialize'), \
                mock.patch.object(InstagramIE, '_real_extract', network_fixture):
            return ytdlp.extract(POST_URL)

    def test_photo_carousel_returns_every_image_in_order(self):
        result = self.extract({'pk': '1234', 'media_type': 8, 'user': {'username': 'fixture'},
                               'carousel_media': [photo('first'), photo('second'), photo('third')]})
        self.assertEqual(result['platform'], 'instagram')
        self.assertEqual([i['kind'] for i in result['items']], ['image'] * 3)
        self.assertIn('/first.jpg?', result['items'][0]['url'])
        self.assertIn('/third.jpg?', result['items'][2]['url'])
        self.assertEqual(len({i['filename'] for i in result['items']}), 3)
        self.assertTrue(all(i['needs_proxy'] for i in result['items']))

    def test_single_photo_keeps_signed_url_and_original_dimensions(self):
        result = self.extract(photo('single'))
        item = result['items'][0]
        self.assertEqual((item['kind'], item['ext'], item['width'], item['height']), ('image', 'jpg', 1600, 2000))
        self.assertTrue(item['url'].endswith('?signature=keep-me'))
        self.assertEqual(headers_for(item['url'])['Referer'], 'https://www.instagram.com/')

    def test_mixed_carousel_keeps_photos_and_videos_without_video_covers(self):
        result = self.extract({'pk': '1234', 'media_type': 8,
                               'carousel_media': [photo('first'), video('clip'), photo('last')]})
        self.assertEqual([i['kind'] for i in result['items']], ['image', 'video', 'image'])
        self.assertTrue(result['items'][1]['url'].endswith('/clip.mp4'))
        self.assertFalse(any('cover.jpg' in i['url'] for i in result['items']))

    def test_video_post_is_still_downloadable_video(self):
        result = self.extract(video('reel'))
        self.assertEqual(result['items'][0]['kind'], 'video')
        self.assertEqual(result['items'][0]['ext'], 'mp4')

    def test_video_without_stream_never_falls_back_to_its_cover(self):
        product = video('broken'); product['video_versions'] = []
        with self.assertRaises(DownloadError):
            self.extract(product)

    def test_largest_sized_photo_candidate_wins_without_rewriting_url(self):
        product = photo('sized')
        product['image_versions2']['candidates'] = [
            {'url': 'https://scontent.cdninstagram.com/small.jpg', 'width': 100, 'height': 200},
            {'url': 'https://scontent.cdninstagram.com/full.jpg?token=original', 'width': 1080, 'height': 2160},
        ]
        item = self.extract(product)['items'][0]
        self.assertEqual(item['url'], 'https://scontent.cdninstagram.com/full.jpg?token=original')
        self.assertEqual((item['width'], item['height']), (1080, 2160))

    def test_image_url_outside_instagram_cdns_is_rejected(self):
        product = photo('invalid')
        product['image_versions2']['candidates'] = [{'url': 'https://cdninstagram.com.attacker.test/photo.jpg'}]
        with self.assertRaises(MediaDownloadError):
            self.extract(product)


if __name__ == '__main__':
    unittest.main()
