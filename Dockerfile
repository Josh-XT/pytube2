FROM python:3.10.12

RUN apt-get update

ENV TZ=Europe/Rome
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /home/khadas/Edge2/Sniper-Bot

# Copia il file requirements.txt nel container
COPY requirements.txt .

# Copia il pacchetto della tua libreria personalizzata nel container
COPY dist/pytube2-15.0.14.tar.gz /tmp/

# Installa le dipendenze e la tua libreria personalizzata in un unico passaggio
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install /tmp/pytube2-15.0.14.tar.gz && \
    rm -rf /root/.cache/pip

# Copia il resto del codice dell'applicazione nella directory di lavoro
COPY . .

# Esponi la porta 8081
EXPOSE 8081

# Specifica il comando per eseguire l'applicazione
CMD ["python3", "Youtube_downloader.py"]
