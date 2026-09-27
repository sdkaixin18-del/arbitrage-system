package main

import (
	"crypto/ecdh"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/netip"
	"strings"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/netstack"
)

func TestUserspaceWireGuardHTTP(t *testing.T) {
	newPeer := func(ip string) (*device.Device, *netstack.Net, *ecdh.PrivateKey) {
		tun, stack, err := netstack.CreateNetTUN([]netip.Addr{netip.MustParseAddr(ip)}, nil, 1280)
		if err != nil {
			t.Fatal(err)
		}
		dev := device.NewDevice(tun, conn.NewDefaultBind(), device.NewLogger(device.LogLevelError, "test: "))
		t.Cleanup(dev.Close)
		key, err := ecdh.X25519().GenerateKey(rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		if err = dev.IpcSet("private_key=" + hex.EncodeToString(key.Bytes()) + "\nlisten_port=0\n"); err != nil {
			t.Fatal(err)
		}
		return dev, stack, key
	}
	server, serverStack, serverKey := newPeer("192.0.2.1")
	client, clientStack, clientKey := newPeer("192.0.2.4")
	if err := server.IpcSet("public_key=" + hex.EncodeToString(clientKey.PublicKey().Bytes()) + "\nallowed_ip=192.0.2.4/32\n"); err != nil {
		t.Fatal(err)
	}
	if err := server.Up(); err != nil {
		t.Fatal(err)
	}
	state, err := server.IpcGet()
	if err != nil {
		t.Fatal(err)
	}
	port := ""
	for _, line := range strings.Split(state, "\n") {
		if strings.HasPrefix(line, "listen_port=") {
			port = strings.TrimPrefix(line, "listen_port=")
		}
	}
	if port == "" || port == "0" {
		t.Fatal("missing listener")
	}
	if err := client.IpcSet(fmt.Sprintf("public_key=%s\nallowed_ip=192.0.2.1/32\nendpoint=127.0.0.1:%s\n", hex.EncodeToString(serverKey.PublicKey().Bytes()), port)); err != nil {
		t.Fatal(err)
	}
	if err := client.Up(); err != nil {
		t.Fatal(err)
	}
	listener, err := serverStack.ListenTCPAddrPort(netip.MustParseAddrPort("192.0.2.1:18767"))
	if err != nil {
		t.Fatal(err)
	}
	httpServer := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "readonly") })}
	defer httpServer.Close()
	go httpServer.Serve(listener)
	transport := &http.Transport{DialContext: clientStack.DialContext}
	defer transport.CloseIdleConnections()
	httpClient := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	response, err := httpClient.Get("http://192.0.2.1:18767/health")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil || string(body) != "readonly" {
		t.Fatal("unexpected tunnel response")
	}
}
