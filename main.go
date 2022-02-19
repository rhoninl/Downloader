package main

import (
	"fmt"
	"io/ioutil"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
)

const (
	userAgent = `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/51.0.2704.103 Safari/537.36`
	blockSize = 10 * 1024 * 1024 // 10M
)

type HttpDownloader struct {
	url           string
	filename      string
	contentLength int
	acceptRanges  bool
	numThreads    int
	split         chan []int
	totalCount    int
	currentCount  int
}

func main() {
	url := ""
	fmt.Println("请输入url")
	fmt.Scanf("%s", &url)
	downloader := New(url, 50)
	downloader.Download()
}

//New 初始化下载器
func New(url string, numThreads int) *HttpDownloader {
	var urlSplits []string = strings.Split(url, "/")
	var filename string = urlSplits[len(urlSplits)-1]
	res, err := http.Head(url)
	if err != nil {
		fmt.Println("[初始化下载器]产生了错误！")
		log.Panicln(err)
	}
	HttpDownloader := new(HttpDownloader)
	HttpDownloader.url = url
	HttpDownloader.contentLength = int(res.ContentLength)
	HttpDownloader.numThreads = numThreads
	HttpDownloader.filename = filename
	HttpDownloader.acceptRanges = len(res.Header["Accept-Ranges"]) != 0 && res.Header["Accept-Ranges"][0] == "bytes"
	HttpDownloader.split = HttpDownloader.Split()
	return HttpDownloader
}

//Download 下载文件调度逻辑
//func (downloader *HttpDownloader) Download() {
//	file, err := os.Create(downloader.filename)
//	if err != nil {
//		fmt.Println("[Download]创建文件失败！")
//		log.Panicln(err)
//	}
//	defer file.Close()
//	if downloader.acceptRanges { //支持多线程下载
//		fmt.Println("该文件支持多线程下载，即将为您进行多线程下载")
//		var wg sync.WaitGroup
//		for _, item := range downloader.Split() {
//			wg.Add(1)
//			go func(start, end int) {
//				defer wg.Done()
//				downloader.SplitDownload(start, end)
//			}(item[0], item[1])
//		}
//		wg.Wait()
//	} else {
//		fmt.Println("该文件不支持多线程下载，即将为您进行单线程下载")
//		resp, err := http.Get(downloader.url)
//		if err != nil {
//			fmt.Println("[Download]发生错误!")
//			log.Panicln(err)
//		}
//		SaveFile(downloader.filename, 0, resp)
//	}
//}
func (downloader *HttpDownloader) Download() {
	file, err := os.Create(downloader.filename)
	if err != nil {
		fmt.Println("[Download]创建文件失败！")
		log.Panicln(err)
	}
	defer file.Close()
	var wg sync.WaitGroup
	for i := 0; i < downloader.numThreads; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case tmp := <-downloader.split:
					downloader.SplitDownload(tmp[0], tmp[1])
				default:
					return
				}
			}
		}()
	}
	wg.Wait()
}

//SplitDownload 文件分片下载
func (downloader *HttpDownloader) SplitDownload(start, end int) {
	downloader.currentCount++
	fmt.Println("正在下载第", downloader.currentCount, "/", downloader.totalCount)
	req, err := http.NewRequest(http.MethodGet, downloader.url, nil)
	if err != nil {
		fmt.Println("[SplitDownload]构建请求时发生错误！")
		log.Panicln(err)
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%v-%v", start, end))
	req.Header.Set("User-Agent", userAgent)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Println("[SplitDownload]发送请求时发生错误！")
		log.Panicln(err)
	}
	defer resp.Body.Close()
	SaveFile(downloader.filename, int64(start), resp)
}

func SaveFile(filename string, offset int64, resp *http.Response) {
	file, err := os.OpenFile(filename, os.O_WRONLY, 0660)
	if err != nil {
		fmt.Println("[SaveFile]打开文件时发生错误")
		log.Panicln(err)
	}
	defer file.Close()
	file.Seek(offset, 0)
	content, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		fmt.Println("[SaveFile]下载文件过程中发生错误")
		log.Panicln(err)
	}
	file.Write(content)
}

//Split 将下载文件分段
func (downloader *HttpDownloader) Split() chan []int {
	blockSizeFin := downloader.contentLength % blockSize
	count := downloader.contentLength/blockSize + 1
	downloader.totalCount = count
	result := make(chan []int, count)
	var start, end int
	for i := 0; i < count-1; i++ {
		start = i * blockSize
		end = (i+1)*blockSize - 1
		result <- []int{start, end}
	}
	result <- []int{end + 1, end + blockSizeFin}
	return result
}
