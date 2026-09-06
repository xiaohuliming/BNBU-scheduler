"""Feed guarded HTTP streams to ffmpeg through anonymous pipes.

ffmpeg receives no remote URLs and only the pipe protocol is enabled, so its
redirect handling cannot bypass the application's network boundary.
"""
import os
import subprocess
import tempfile
import threading

from . import http
from .transport import MediaDownloadError, headers_for

_slots = threading.BoundedSemaphore(3)
_MAX_INPUT = 1024 * 1024 * 1024


class MergeBusyError(MediaDownloadError):
    pass


def merge_chunks(ffmpeg, video, audio, *, referer, proxies_for, user_agent):
    if not _slots.acquire(blocking=False):
        raise MergeBusyError('合并任务较多，请稍后重试。')
    responses, pipes, workers, errors = [], [], [], []
    process = None
    timer = None
    stop = threading.Event()
    stderr = None

    def pump(response, descriptor):
        try:
            with os.fdopen(descriptor, 'wb') as destination:
                count = 0
                for chunk in response.iter_content(64 * 1024):
                    if stop.is_set():
                        break
                    count += len(chunk)
                    if count > _MAX_INPUT:
                        raise MediaDownloadError('媒体文件超过单项 1 GB 合并限制。')
                    destination.write(chunk)
        except Exception as exc:
            if not stop.is_set():
                errors.append(exc)
        finally:
            response.close()

    try:
        stderr = tempfile.TemporaryFile()
        for url in (video, audio):
            headers = {'User-Agent': user_agent, 'Accept-Encoding': 'identity'}
            if referer:
                headers['Referer'] = referer
            headers.update(headers_for(url))
            headers['Accept-Encoding'] = 'identity'
            response = http.get(url, headers=headers, proxies=proxies_for(url),
                                stream=True, timeout=(10, 30))
            responses.append(response)
            response.raise_for_status()
            if int(response.headers.get('Content-Length') or 0) > _MAX_INPUT:
                raise MediaDownloadError('媒体文件超过单项 1 GB 合并限制。')
            if 'text/' in response.headers.get('Content-Type', ''):
                raise MediaDownloadError('源站没有返回媒体文件，请重新解析。')
            pipes.append(list(os.pipe()))

        cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin']
        for reader, _ in pipes:
            cmd += ['-protocol_whitelist', 'pipe', '-i', f'pipe:{reader}']
        cmd += ['-map', '0:v:0', '-map', '1:a:0', '-c', 'copy',
                '-movflags', 'frag_keyframe+empty_moov', '-f', 'mp4', 'pipe:1']
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr,
                                   pass_fds=tuple(pair[0] for pair in pipes))
        for response, pair in zip(responses, pipes):
            os.close(pair[0])
            pair[0] = None
            worker = threading.Thread(target=pump, args=(response, pair[1]), daemon=True)
            worker.start()
            workers.append(worker)
            pair[1] = None  # owned by the worker now

        def expire():
            if process.poll() is None:
                errors.append(MediaDownloadError('合并超时，请稍后重试。'))
                process.kill()

        timer = threading.Timer(15 * 60, expire)
        timer.daemon = True
        timer.start()
        while chunk := process.stdout.read(64 * 1024):
            yield chunk
        process.wait(timeout=10)
        for worker in workers:
            worker.join(timeout=1)
        if errors:
            raise errors[0]
        if process.returncode:
            raise MediaDownloadError('音视频合并失败，请重新解析，或分别下载画面与音频。')
    finally:
        stop.set()
        if timer:
            timer.cancel()
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
        for pair in pipes:
            for descriptor in pair:
                if descriptor is not None:
                    os.close(descriptor)
        for response in responses:
            response.close()
        for worker in workers:
            worker.join(timeout=1)
        if stderr is not None:
            stderr.close()
        _slots.release()
