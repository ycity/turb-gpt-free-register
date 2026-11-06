#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把容器网段的 172.19.0.1:8215 转发到 sub2api 的 127.0.0.1:8213。

sub2api 只监听 127.0.0.1，注册系统跑在 Docker 里访问不到。
改 sub2api 的监听地址要重启它，而它正在服务中；这里用一个独立的
转发进程打通，不碰 sub2api 本身，随时可以停掉。
"""
import socket
import threading
import sys

LISTEN = ("172.19.0.1", 8215)
TARGET = ("127.0.0.1", 8213)


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client):
    try:
        upstream = socket.create_connection(TARGET, timeout=15)
    except OSError:
        client.close()
        return
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(128)
    print("转发就绪 %s:%s -> %s:%s" % (*LISTEN, *TARGET), flush=True)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            continue
        conn.settimeout(None)
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
