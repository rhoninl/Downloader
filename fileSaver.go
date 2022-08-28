package main

import (
	"context"
	"io"
	"log"
	"net/http"
	"os"
	"sync"
)

type saver struct {
	filename string
	file     *os.File
}

func newSaver(ctx context.Context, filename string, data <-chan saveData, callBack chan<- int, wg *sync.WaitGroup) {
	defer wg.Done()
	thisSaver := &saver{
		filename: filename,
	}

	file, err := os.OpenFile(filename, os.O_RDWR, 0644)
	if err != nil {
		log.Println("cannot open file,error: ", err)
		panic(err)
	}

	thisSaver.file = file
	thisSaver.saveFile(ctx, data, callBack)
}

func (saver *saver) saveFile(ctx context.Context, data <-chan saveData, callBack chan<- int) {
	for {
		select {
		case info := <-data:
			saver.save(info)
			callBack <- info.position
		case <-ctx.Done():
			return
		}
	}
}

func (saver *saver) save(data saveData) {
	saver.file.WriteAt(data.data, int64(data.position))
	saver.file.Sync()
}

func saveSingleFile(filename string, resp *http.Response) {
	file, err := os.OpenFile(filename, os.O_WRONLY, 0660)
	if err != nil {
		file, err = os.Create(filename)
		if err != nil {
			log.Println("cannot create file to save")
			panic(err)
		}
	}
	defer file.Close()
	content, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Println("cannot read byte from response")
		return
	}
	file.Write(content)
}
