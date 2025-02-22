import os
import re
import time
import shutil
import subprocess
from pathlib import Path
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from bs4 import BeautifulSoup
import yt_dlp  # Using yt_dlp for YouTube video downloading
from flask_socketio import SocketIO, emit
import re
import instaloader
from urllib.parse import urlparse, parse_qs

app = Flask(__name__)
CORS(app, origins=["http://localhost:3000"])


# Initialize Flask-SocketIO
socketio = SocketIO(app,cors_allowed_origins="*")

# Global variable to track download progress
download_progress = 0

# Directory to store temporary downloads
DOWNLOAD_FOLDER = Path("downloads")
DOWNLOAD_FOLDER.mkdir(exist_ok=True)



# Initialize Instaloader object
L = instaloader.Instaloader()


@app.route("/")
def home():
    return "Hello, World!"


@app.route('/download_reels', methods=['post'])
def download_reels():
    """Flask route to download Instagram Reels video."""
    # Get the URL from query parameters
    url = request.args.get('url')
    
    if not url:
        return "Error: Missing URL parameter.", 400
    
    try:
        # Extract the short URL from the provided URL
        post = instaloader.Post.from_url(L.context, url)
        
        # Create a folder to store the downloaded reels if it doesn't exist
        download_folder = 'downloads/instagram_reels'
        if not os.path.exists(download_folder):
            os.makedirs(download_folder)
        
        # Define the path to save the video
        video_path = os.path.join(download_folder, f"{post.shortcode}.mp4")
        
        # Download the Reels video to the specified path
        L.download_post(post, target=download_folder)
        
        # Return the video file to the client
        return send_file(video_path, as_attachment=True)
    
    except Exception as e:
        return f"Error downloading Reels: {e}", 500



@app.route('/submitLink', methods=['POST'])
def submit_link():
    """Endpoint to submit a YouTube link and retrieve video details."""
    url = request.json.get('url')
    try:
        video_details = get_youtube_video_details(url)
        return jsonify(video_details)
    except Exception as error:
        return jsonify({'error': str(error)}), 500

@app.route('/videoDetails', methods=['POST'])
def video_details():
    """Endpoint to retrieve video details and download the video with the desired quality."""
    try:
        folder_path = 'downloads'

        # Delete the existing folder if it exists
        if os.path.exists(folder_path):
            shutil.rmtree(folder_path)

        # Recreate the folder
        os.makedirs(folder_path)
        data = request.json
        video_url=remove_playlist_from_url(data.get("url"))
        desired_quality = data.get("quality")  # e.g., "480p"

        if not video_url or not desired_quality:
            return jsonify({"error": "URL and Quality parameters are required"}), 400

        # Extract video quality number (e.g., 720 from "720p")
        quality_number = int(re.sub(r'\D', '', desired_quality))

        # Fetch video info from YouTube
        with yt_dlp.YoutubeDL() as ydl:
            info_dict = ydl.extract_info(video_url, download=False)
            video_id = info_dict.get('id')
            formats = info_dict.get('formats', [])

        # Select the best video format
        video_format_id = next(
            (f['format_id'] for f in formats if f.get('height') == quality_number and 'vp' in f.get('vcodec', '')),
            None
        ) or next(
            (f['format_id'] for f in formats if f.get('height') <= quality_number and 'vp' in f.get('vcodec', '')),
            None
        )

        if not video_format_id:
            return jsonify({"error": f"No available format for requested quality {desired_quality}."}), 404

        video_file_path = DOWNLOAD_FOLDER / f"{video_id}_video.mp4"
        audio_file_path = DOWNLOAD_FOLDER / f"{video_id}_audio.mp4"
        merged_file_path = DOWNLOAD_FOLDER / f"{video_id}_merged.mp4"


        # Download video (0% to 50%)
        socketio.emit("download_progress", {"progress": 1})
        # Download video
        ydl_video_opts = {
            'format': video_format_id,
            'outtmpl': str(video_file_path),
            'noplaylist': True,
            'progress_hooks': [lambda d: progress_hook(d, 0, 50)],
        }
        with yt_dlp.YoutubeDL(ydl_video_opts) as ydl:
            ydl.download([video_url])


         # Download audio (50% to 70%)
        socketio.emit("download_progress", {"progress": 50})
        # Download audio
        ydl_audio_opts = {
            'format': 'bestaudio',
            'outtmpl': str(audio_file_path),
            'noplaylist': True,
            'progress_hooks': [lambda d: progress_hook(d, 50, 70)],
        }
        with yt_dlp.YoutubeDL(ydl_audio_opts) as ydl:
            ydl.download([video_url])

        # Merge video and audio (70% to 100%)
        socketio.emit("download_progress", {"progress": 70})

        # Merge video and audio using FFmpeg
        merge_command = [
            "ffmpeg", "-i", str(video_file_path), "-i", str(audio_file_path),
            "-c:v", "copy", "-c:a", "aac", "-strict", "experimental", str(merged_file_path)
        ]
        subprocess.run(merge_command, check=True)

        socketio.emit("download_progress", {"progress": 100})

        # Send merged file to client
        response = send_file(str(merged_file_path), as_attachment=True, download_name=f"{video_id}_merged.mp4")

        # # Cleanup temporary files
        # cleanup_files(video_file_path, audio_file_path, merged_file_path)

        return response

    except Exception as e:
         # Cleanup temporary files
        cleanup_files(video_file_path, audio_file_path, merged_file_path)

        return jsonify({"error": str(e)}), 500



def progress_hook(d, start, end):
    """Hook for tracking download progress in different stages."""
    if d['status'] == 'downloading' and 'downloaded_bytes' in d and 'total_bytes' in d:
        percent = d['downloaded_bytes'] / d['total_bytes']
        progress = start + (percent * (end - start))  # Scale to custom range
        socketio.emit("download_progress", {"progress": round(progress, 2)})



def get_youtube_video_details(video_link):
    """Retrieve video details such as title, duration, and available formats from a YouTube link."""
    with yt_dlp.YoutubeDL() as ydl:
        try:
            url=remove_playlist_from_url(video_link)
            info_dict = ydl.extract_info(url, download=False)
            video_id = info_dict['id']
            title = info_dict['title']
            duration = info_dict['duration']

            # Extract video qualities and related details
            video_qualities = []
            for f in info_dict.get('formats', []):
                video_qualities.append({
                    'qualityLabel': f.get('format_note', 'N/A'),
                    'hasVideo': f.get('vcodec', 'none') != 'none',
                    'hasAudio': f.get('acodec', 'none') != 'none',
                    'container': f.get('ext', 'unknown'),
                    'audioCodec': f.get('acodec', 'unknown'),
                    'videoCodec': f.get('vcodec', 'unknown'),
                    'url': f.get('url')
                })

            thumbnail_url = f"https://img.youtube.com/vi/{video_id}/0.jpg"  # Thumbnail URL

            return {
                'videoId': video_id,
                'title': title,
                'thumbnailUrl': thumbnail_url,
                'duration': duration,
                'videoQualities': video_qualities,
                'videoLink': video_link
            }
        except Exception as error:
            raise Exception('Failed to retrieve video details')

def cleanup_files(video_file_path, audio_file_path, merged_file_path):
    """Remove video, audio, and merged files after use."""
    try:
        for file_path in [video_file_path, audio_file_path, merged_file_path]:
            if file_path.exists():
                file_path.unlink()
    except Exception as e:
        print(f"Error during cleanup: {e}")


def is_playlist_url(url):
    """Check if the URL is a YouTube playlist URL."""
    # Regular expression to check if the URL contains the 'list' parameter
    return 'list' in parse_qs(urlparse(url).query)

def remove_playlist_from_url(playlist_url):
    """
    Removes the playlist from a YouTube URL and returns a single video URL if it's a playlist.
    """
    # Check if the URL is a playlist
    if is_playlist_url(playlist_url):
        # Parse the URL to extract query parameters
        parsed_url = urlparse(playlist_url)
        
        # Extract video ID from the 'v' parameter
        video_id = parse_qs(parsed_url.query).get('v', [None])[0]
        
        # If video ID exists, return the single video URL
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        else:
            raise ValueError("Invalid YouTube playlist URL, video ID not found.")
    else:
        # If it's not a playlist URL, return the original URL
        return playlist_url


if __name__ == '__main__':
    socketio.run(app, port=5000)  # Run the Flask application with WebSocket
