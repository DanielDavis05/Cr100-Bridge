"""Decode actual served image fallbacks, including HTTP and corrupt-image failures."""
import http.server
import io
import threading
from types import SimpleNamespace
import unittest

from PIL import Image

from radio_playback import RadioArtStore


class ArtworkFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests=[]
        cls.images={}
        for color in ('red','blue'):
            output=io.BytesIO();Image.new('RGB',(400,240),color).save(output,'PNG')
            cls.images['/'+color]=output.getvalue()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path)
                if self.path in cls.images:
                    data=cls.images[self.path];self.send_response(200)
                elif self.path=='/invalid':data=b'<html>not an image</html>';self.send_response(200)
                else:data=b'not found';self.send_response(404)
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:%d'%cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(2)

    def picture(self,store,source,fallback=None):
        path=store.path_for(source,prefetch=False,fallback=fallback)
        data=store.get(path.rsplit('/',1)[-1][:-4])
        self.assertIsNotNone(data)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format,'JPEG');self.assertLessEqual(max(image.size),200)
            self.assertFalse(image.info.get('progressive',False))
            return image.convert('RGB').getpixel((0,0))

    def test_http_404_uses_station_logo_with_one_image_decode_and_cached_result(self):
        store=RadioArtStore(log=lambda _:None);prepare=store.backend.prepare;decoded=[]
        store.backend.prepare=lambda data:decoded.append(len(data)) or prepare(data)
        before=len(self.requests)
        pixel=self.picture(store,self.base+'/missing',self.base+'/red')
        self.assertGreater(pixel[0],200);self.assertLess(pixel[2],40)
        self.assertEqual(self.requests[before:],['/missing','/red'])
        self.assertEqual(len(decoded),1)
        self.picture(store,self.base+'/missing',self.base+'/red')
        self.assertEqual(self.requests[before:],['/missing','/red'])

    def test_corrupt_cover_body_uses_valid_station_logo(self):
        pixel=self.picture(RadioArtStore(log=lambda _:None),self.base+'/invalid',self.base+'/blue')
        self.assertGreater(pixel[2],200);self.assertLess(pixel[0],40)

    def test_missing_logo_uses_packaged_neutral_image(self):
        self.picture(RadioArtStore(log=lambda _:None),self.base+'/missing',self.base+'/also-missing')

    def test_shared_missing_cover_keeps_each_stations_own_logo(self):
        store=RadioArtStore(log=lambda _:None)
        red=self.picture(store,self.base+'/missing',self.base+'/red')
        blue=self.picture(store,self.base+'/missing',self.base+'/blue')
        self.assertGreater(red[0],red[2]);self.assertGreater(blue[2],blue[0])

    def test_failed_backend_art_proxy_falls_back_to_logo(self):
        store=RadioArtStore(log=lambda _:None)
        source=store.backend.backend_picture(SimpleNamespace(get=lambda key:None),'/art/missing.jpg')
        pixel=self.picture(store,source,self.base+'/red')
        self.assertGreater(pixel[0],200)


if __name__=='__main__':unittest.main()
