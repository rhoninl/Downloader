package main

import (
	"fmt"
	"io/ioutil"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

var (
	blockSize   = 30 * 1024 // 30kB
	runtineNum  = 20
	sizeUnitMap = []string{"B", "kiB", "MiB", "GiB", "TiB", "PiB"}
	timeUnitMap = []string{"s", "min", "h"}
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
	file          *os.File
	start         time.Time
}

func main() {
	var url string
	fmt.Println("请输入url")
	fmt.Scanf("%s", &url)
	downloader := New(url, runtineNum)
	downloader.Download()
}

//New 初始化下载器
func New(url string, numThreads int) *HttpDownloader {
	var urlSplits []string = strings.Split(url, "/")
	var filename string = strings.Split(urlSplits[len(urlSplits)-1], "?")[0]
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
	HttpDownloader.start = time.Now()
	return HttpDownloader
}

// Download 下载文件调度逻辑
// func (downloader *HttpDownloader) Download() {
// 	file, err := os.Create(downloader.filename)
// 	if err != nil {
// 		fmt.Println("[Download]创建文件失败！")
// 		log.Panicln(err)
// 	}
// 	defer file.Close()
// 	if downloader.acceptRanges { //支持多线程下载
// 		fmt.Println("该文件支持多线程下载，即将为您进行多线程下载")
// 		var wg sync.WaitGroup
// 		for _, item := range downloader.Split() {
// 			wg.Add(1)
// 			go func(start, end int) {
// 				defer wg.Done()
// 				downloader.SplitDownload(start, end)
// 			}(item[0], item[1])
// 		}
// 		wg.Wait()
// 	} else {
// 		fmt.Println("该文件不支持多线程下载，即将为您进行单线程下载")
// 		resp, err := http.Get(downloader.url)
// 		if err != nil {
// 			fmt.Println("[Download]发生错误!")
// 			log.Panicln(err)
// 		}
// 		SaveFile(downloader.filename, 0, resp)
// 	}
// }

func (downloader *HttpDownloader) Download() {
	file, err := os.Create(downloader.filename)
	if err != nil {
		fmt.Println("[Download]创建文件失败！")
		log.Panicln(err)
	}
	defer file.Close()
	downloader.file = file
	var wg sync.WaitGroup
	go func() {
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
	}()
	var pert int
	var size, lastSize, speed int64
	var download = "download        "
	var tiao = "[__________________________________________________]"
	for i := 0; pert < 100; i++ {
		select {
		case <-time.Tick(time.Second):
			tmpDownload := strings.Replace(download, " ", ".", i%4)
			info, _ := file.Stat()
			size, speed, lastSize = info.Size(), size-lastSize, size
			pert = int(size) * 100 / downloader.contentLength
			tmpTiao := strings.Replace(strings.Replace(tiao, "_", "=", pert/2), "_", ">", 1)
			fmt.Printf("\t%s\t%s\t%s\t %d%% / 100%% \t%11s/s\t%s               \r", tmpDownload, file.Name(), tmpTiao, pert, sizeUnitSuitable(int(speed)), timeUnitSuitable(float64(i)))
		}
	}
	wg.Wait()
	diff := time.Since(downloader.start).Seconds()
	fmt.Println()
	fmt.Println("\t download Success! filename:", file.Name(), "\tfileSize:", sizeUnitSuitable(downloader.contentLength))
	log.Printf("\t totalTime: %s\t averageSpeed:%s/s\n", timeUnitSuitable(diff), sizeUnitSuitable(downloader.contentLength/int(diff)))
}

//SplitDownload 文件分片下载
func (downloader *HttpDownloader) SplitDownload(start, end int) {
	req, err := http.NewRequest(http.MethodGet, downloader.url, nil)
	if err != nil {
		fmt.Println("[SplitDownload]构建请求时发生错误！")
		log.Panicln(err)
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%v-%v", start, end))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Println("[SplitDownload]发送请求时发生错误！")
		log.Panicln(err)
	}
	SaveFile(downloader.file, int64(start), resp)
}

func SaveFile(file *os.File, offset int64, resp *http.Response) {
	file.Seek(offset, 0)
	content, err := ioutil.ReadAll(resp.Body)
	defer resp.Body.Close()
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

func sizeUnitSuitable(data int) string {
	i := 0
	var dataF = float64(data)
	for i = 0; dataF > 1024; i++ {
		dataF /= 1024
	}
	return fmt.Sprintf("%.2f %s", dataF, sizeUnitMap[i])
}

func timeUnitSuitable(data float64) string {
	i := 0
	var dataF = data
	for i = 0; dataF > 60; i++ {
		dataF /= 60
	}
	zheng := int(dataF)
	xiao := int((dataF - float64(zheng)) * 100)
	if i == 0 {
		return fmt.Sprintf("%02d %s", zheng, timeUnitMap[i])
	}
	return fmt.Sprintf("%d:%02d %s", zheng, xiao, timeUnitMap[i])
}
