package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"sync"
)

type downloader struct {
	url string
}

func newDownloaderSingle(ctx context.Context, url string, filename string) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		log.Println("error when build request")
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Println("cannot send request to target url")
		return
	}
	saveSingleFile(filename, resp)
}

func newDownloaderSplite(ctx context.Context, url string, chanSplit <-chan []int, collector chan<- saveData, wg *sync.WaitGroup) {
	defer wg.Done()
	downloader := &downloader{url: url}
	for {
		select {
		case info := <-chanSplit:
			downloader.downloadSplite(ctx, info[0], info[1], collector)
		case <-ctx.Done():
			return
		default:
			return
		}
	}
}

func (d *downloader) downloadSplite(ctx context.Context, start int, end int, collector chan<- saveData) {
	req, err := http.NewRequest(http.MethodGet, d.url, nil)
	if err != nil {
		fmt.Println("error when build request")
		log.Panicln(err)
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%v-%v", start, end))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Println("cannot read Info From body")
	}
	defer resp.Body.Close()

	content, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Println("cannot read from body")
	}

	collector <- saveData{
		data:     content,
		position: start,
	}
}
