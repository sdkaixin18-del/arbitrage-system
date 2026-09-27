// A fixed-target userspace WireGuard bridge, without changing macOS routes.
package main

import (
	"context"
	"crypto/ecdh"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/netstack"
)

type Config struct {
	PrivateKey string
	ServerKey  string
	Token      string
}

func main() {
	path := flag.String("config", "", "Protected configuration file")
	endpoint := flag.String("endpoint", "", "Cloud WireGuard IP:port")
	initKey := flag.String("init-server-key", "", "Initialize keys using server public key")
	flag.Parse()
	if *path == "" {
		log.Fatal("config required")
	}
	if *initKey != "" {
		server, err := base64.StdEncoding.DecodeString(*initKey)
		if err != nil || len(server) != 32 {
			log.Fatal("invalid server key")
		}
		key, err := ecdh.X25519().GenerateKey(rand.Reader)
		if err != nil {
			log.Fatal(err)
		}
		secret := make([]byte, 32)
		if _, err = rand.Read(secret); err != nil {
			log.Fatal(err)
		}
		cfg := Config{hex.EncodeToString(key.Bytes()), hex.EncodeToString(server), hex.EncodeToString(secret)}
		file, err := os.OpenFile(*path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			log.Fatal(err)
		}
		if err = json.NewEncoder(file).Encode(cfg); err != nil {
			log.Fatal(err)
		}
		file.Close()
		fmt.Println(base64.StdEncoding.EncodeToString(key.PublicKey().Bytes()))
		return
	}
	if _, err := netip.ParseAddrPort(*endpoint); err != nil {
		log.Fatal("numeric WireGuard endpoint required")
	}
	info, err := os.Stat(*path)
	if err != nil || info.Mode().Perm()&0077 != 0 {
		log.Fatal("config must be owner-only")
	}
	data, err := os.ReadFile(*path)
	if err != nil {
		log.Fatal(err)
	}
	var cfg Config
	if err = json.Unmarshal(data, &cfg); err != nil {
		log.Fatal("invalid config")
	}
	tun, stack, err := netstack.CreateNetTUN([]netip.Addr{netip.MustParseAddr("192.0.2.4")}, nil, 1280)
	if err != nil {
		log.Fatal(err)
	}
	dev := device.NewDevice(tun, conn.NewDefaultBind(), device.NewLogger(device.LogLevelError, "depth-trial: "))
	defer dev.Close()
	err = dev.IpcSet(fmt.Sprintf("private_key=%s\npublic_key=%s\nallowed_ip=192.0.2.1/32\nendpoint=%s\npersistent_keepalive_interval=25\n", cfg.PrivateKey, cfg.ServerKey, *endpoint))
	if err != nil {
		log.Fatal("WireGuard config rejected")
	}
	if err = dev.Up(); err != nil {
		log.Fatal(err)
	}
	target, _ := url.Parse("http://192.0.2.1:18767")
	transport := &http.Transport{DialContext: stack.DialContext, MaxIdleConns: 4,
		MaxIdleConnsPerHost: 4, MaxConnsPerHost: 4, IdleConnTimeout: 60 * time.Second,
		ResponseHeaderTimeout: 6 * time.Second}
	defer transport.CloseIdleConnections()
	proxy := &httputil.ReverseProxy{Rewrite: func(r *httputil.ProxyRequest) { r.SetURL(target) }, Transport: transport,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			http.Error(w, "WireGuard path unavailable", 502)
		}}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawQuery != "" || !((r.Method == "GET" && r.URL.Path == "/health") || (r.Method == "POST" && r.URL.Path == "/v1/public-get")) {
			http.NotFound(w, r)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 4096)
		ctx, cancel := context.WithTimeout(r.Context(), 7*time.Second)
		defer cancel()
		proxy.ServeHTTP(w, r.WithContext(ctx))
	})
	server := &http.Server{Addr: "127.0.0.1:18867", Handler: handler, ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout: 3 * time.Second, WriteTimeout: 8 * time.Second, IdleTimeout: 60 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		server.Shutdown(shutdown)
	}()
	log.Print("readonly bridge listening on 127.0.0.1:18867")
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
