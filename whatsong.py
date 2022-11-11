# This script is targeting Python 2.7.16 because that's what my Raspberry Pi server is using
from __future__ import unicode_literals #Needed by youtube-dl for Python vers under 3.x
import tweepy
import ffmpeg
import requests
import base64
import math
import random
import time
import re
import youtube_dl
#from os.path import exists #If you see this in the future its safe to delete lol
from rdb import rdb

"""
	List of error codes I'm interested in so far:

	144: Tweet was deleted
	63: User was banned
	34: Page does not exist (When does this proc vs 144???)
	136: You have been blocked by the author of this tweet
	179: You are not authorized to view status (Privated acc?)
	50: User not found
	433: User restricted who can reply to the tweet. Extremely rare, but possible even non-maliciously
"""

def main():

	#Get the authentication credentials to connect to twitter
	try:
		keysFile = open("keys", 'r') #Try to assume it is in the cwd
	except IOError as e:
		print("Couldn't find keys file, falling back to absolute path")
		keysFile = open("/home/pi/project/whatsong/keys") #Try an absolute path based on where it should be on my Raspberry Pi

	keys = keysFile.read().split('\n')
	APIKey = keys[0].split('=')[1]
	APISecretKey = keys[1].split('=')[1]
	AccessToken = keys[2].split('=')[1]
	AccessSecretToken = keys[3].split('=')[1]
	global RapidAPIKey #Read in shazam post request function
	RapidAPIKey = keys[5].split('=')[1]
	keysFile.close()

	# Authenticate to Twitter using your app
	auth = tweepy.OAuthHandler(APIKey, APISecretKey)
	auth.set_access_token(AccessToken, AccessSecretToken)

	api = tweepy.API(auth)
	#Note to self: you can use wait_on_rate_limit=True, and wait_on_rate_limit_notify=True to print when tweepy is blocking and to block when 
	#twitter is being bothered too much by your app

	while(True):
		try:
			api.verify_credentials()
			print("Authentication OK, connected to Twitter")
			break
		except:
			print("Error during authentication")
			time.sleep(50)

	#Get the previously completed jobs off of redis
	serviced = rdb.getRawJobs()
	print("Got raw jobs, length was: ", len(serviced))

	#Main execution loop
	while(True):
		try:
			mentions = getNewMentions(api.mentions_timeline(count=99, tweet_mode="extended"), serviced) #Filter through all mentions to get only potentially valid ones
		except tweepy.TweepError as e:
			print("Tweepy error occurred when trying to get my bots mentions:{}".format(e))
			time.sleep(60)
			continue

		for mention in mentions: 
			if handleMention(api, mention, serviced):
				time.sleep(15) #Wait 15 seconds between jobs, was 30 previously. Don't bother waiting if previous mention flopped


def getTimestamp(tweet): #Checks if a timestamp was included and if so, returns it. Otherwise returns None
	txt = tweet.full_text
	if re.search("[0-9]:[0-9][0-9]", txt) is not None:
		return re.search("[0-9]:[0-9][0-9]", txt).group() #group method selects only the part that matched
	else:
		return None

#Finish off a job, tweeting it out if shazam found it or if shazam confirmed it couldn't find it. Any other situation like ffmpeg failing -> skip the tweet
def wrapUpJob(api, askerName, asker, result, serviced):
	#We always write our job to redis and append it to the in-memory list
	rdb.writeJob(askerName, str(asker), result)
	serviced.append(str(asker)) 
	if result is None: return #Guard against sending a tweet if it failed

	try:
		api.update_status("@" + askerName + " " + result, in_reply_to_status_id=asker)
	except tweepy.RateLimitError as e:
		print("Twitter api rate limit reached".format(e))
		time.sleep(90)
		api.update_status("@" + askerName + " " + result, in_reply_to_status_id=asker)
	except tweepy.TweepError as e:
		if e.api_code == 385:
			print("Someone managed to request me, and only went private or blocked me JUST before I tweeted them back. Error 385")

#This is the main execution loop of the bot, meant to be called indefinitely
def handleMention(api, mention, serviced):
	unknownUsername = "UNKNOWN"
	vidAndAsker = processMention(api, mention)
	vid = vidAndAsker[0] #Stores whichever tweet id posted the actual video
	asker = vidAndAsker[1] #Stores whichever tweet id asked for help
	link = vidAndAsker[2] #Stores whether or not it was a link instead of a video
	timestamp = vidAndAsker[3] #Stores a timestamp if one was given
	if vid == -2:
		print("someoneNeedsMe mention marked as done due to error proc or some other thing (this is intentional!)")
		wrapUpJob(api, unknownUsername, asker, None, serviced)
		return False

	if vid != -1:
		try:
			askerName = api.get_status(asker).user.screen_name
			goodwav = downloadToGoodWav(api, vid, link, timestamp)
			if goodwav == -1:   wrapUpJob(api, unknownUsername, asker, None, serviced)
			elif goodwav == -2: pass #Something bad happened (at this point most likely a server side issue so try again)
			else:
				payload = toBase64(goodwav)
				result = shazam(payload)
				wrapUpJob(api, askerName, asker, result, serviced)
				print("@" + askerName + ": " + result)
				return True
		except tweepy.TweepError as e:
			print("Tweepy error occurred when trying to finish processing a request:{}".format(e))
			if e.api_code == 144 or e.api_code == 63:
				wrapUpJob(api, unknownUsername, asker, None, serviced) #If error is that status didn't exist, it was probably deleted. Fake adding it to serviced, and continue on.
	return False

#For each mention, processes it to extract the data the bot needs to do its job
def processMention(api, mention):
	try:
		if hasattr(mention, "extended_entities") and mention.extended_entities["media"][0]['type'] == "video":
			#If you are in this block, they must have posted a video and you haven't replied to them yet.
			timestamp = getTimestamp(mention)
			return [mention.id, mention.id, None, timestamp]
		elif mention.in_reply_to_status_id is not None and hasattr(api.get_status(mention.in_reply_to_status_id, tweet_mode="extended"), "extended_entities") and \
			 api.get_status(mention.in_reply_to_status_id, tweet_mode="extended").extended_entities["media"][0]['type'] == "video":
			#If you are in this block, the person they are replying to must have posted a video and you haven't replied to them yet
			timestamp = getTimestamp(api.get_status(mention.in_reply_to_status_id, tweet_mode="extended"))
			return [mention.in_reply_to_status_id, mention.id, None, timestamp]
		elif len(mention.entities["urls"]) > 0:
			#If you are in this block, the person posted a link and you haven't replied to them yet
			timestamp = getTimestamp(mention)
			return [mention.id, mention.id, mention.entities["urls"][0]["expanded_url"], timestamp]
	except tweepy.RateLimitError as e:
		print("Twitter api rate limit reached".format(e))
		time.sleep(60)
	except tweepy.TweepError as e:
		errorCodes = [136, 144, 63, 34, 179, 50, 433]
		print("Tweepy error occurred while in someoneNeedsMe:{}".format(e))
		#Attempt to snatch video url even if original poster blocked us
		if e.api_code == 136 and ((hasattr(mention, "extended_entities") and mention.extended_entities["media"][0]['type'] == "video") is False): return snatchVideoURL()
		if e.api_code in errorCodes: return [-2, mention.id, -2, -2]
	except AttributeError as e:
		print(e)
		print("Triggered on Tweet: " + str(mention.id))
		return [-2, mention.id, -2, -2] #Not sure if this job can be salvaged, so I will ditch it as a precaution.
	return [-1, -1, -1, -1]

def getNewMentions(mentions, serviced):
	return [mention for mention in mentions if str(mention.id) not in serviced and str(mention.id) + "\r" not in serviced and mention.user.screen_name != "watsongisthis"]

def snatchVideoURL(mention):
	timestamp = getTimestamp(mention)
	url = scrapeStatusForVideo("https://twitter.com/i/status/" + str(mention.in_reply_to_status_id))
	if(url is None): return [-2, mention.id, -2, -2]
	return [mention.in_reply_to_status_id, mention.id, url, timestamp]

def downloadToGoodWav(api, theid, url, timestamp): #Given a status id of a tweet containing an mp4, extracts the url to that mp4, downloads 4 seconds of it, converts to signed 16bit le 44,100Hz Mono wav file and returns that
	if(url is None):
		try:
			if hasattr(api.get_status(theid, tweet_mode="extended"), "extended_entities") and api.get_status(theid, tweet_mode="extended").extended_entities["media"][0]['type'] == "video":
				maxVal = 0
				best = None
				for opt in api.get_status(theid, tweet_mode="extended").extended_entities["media"][0]['video_info']['variants']:
					if 'bitrate' in opt and int(opt['bitrate']) > maxVal:
						maxVal = int(opt['bitrate'])
						best = api.get_status(theid, tweet_mode="extended").extended_entities["media"][0]['video_info']['variants'].index(opt)
				url = api.get_status(theid, tweet_mode="extended").extended_entities["media"][0]['video_info']['variants'][best]['url']
			else:
				print("No extended_entities were found from this status")
				url = None
				return None
		except:
			print("Something bad happened, most likely 503 error or something on twitter's end. Returning none")
			return None
	try:
		probe = ffmpeg.probe(url)
		duration = math.floor(float(probe["format"]["duration"]))
		if timestamp is not None and isGoodTimestamp(duration, timestamp): #timestamp was given and its a valid one
			ts = timestamp.split(":")
			startTime = int(ts[0])*60 + int(ts[1]) #Convert minutes to seconds and add to seconds
		else: #Either no timestamp given, or it was a bogus one. Try anyways at middle of video
			startTime = ((duration/2)-2) #Divide by 2, subtract two, use the next four seconds of footage
			if startTime < 0: startTime = 0
		raw = ffmpeg.input(url, ss=startTime, t=4) #A 2:20 length mp4 I found on twitter was 12.3MB and this limit is 20MB (20971520)
		audio = raw.audio #Set audio stream to be the audio portion of the input (aka raw)
		out = ffmpeg.output(audio, 'goodwav.wav', ac=1, ar=44100) #Create output using options, filename, and a single audio stream (no video)
		ffmpeg.run(out, overwrite_output=True, quiet=True, capture_stderr=True) #Actually generate the output
	except ffmpeg._run.Error as e:
		print("run error. Supplied url most likely not a video")
		print('stderr: ', e.stderr.decode('utf8'))
		return -1
	except tweepy.TweepError as e:
		print("Most likely an error occurred due to url being bad or twitter being down")
		return -2
	return 'goodwav.wav'

def isGoodTimestamp(duration, timestamp): #Returns true if a usable valid timestamp, false if malicious or just bad timestamp supplied
	ts = timestamp.split(":")
	timeGiven = int(ts[0])*60 + int(ts[1])
	if timeGiven >= duration: return False
	else: return True

def isLastMention(extendedstatus): #Parses all @ mentions in an extended status, and returns true only if @watsongisthis was the last mention.
	text = extendedstatus.full_text
	m = re.findall("(?<!\w)(@\S+)", text) #This should match only "@____", that do not have anything before the @ except a whitespace or literally nothing.
	if m[-1] == "@watsongisthis":
		return True
	return False

def toBase64(sample): #Accepts 44,100 Hz 16 bit signed PCM mono audio file, converts to base64 and returns that as a string
	if sample is None:
		return None
	try:
		samplefile = open(sample, "rb").read()
		b64sample = base64.b64encode(samplefile)
	except:
		print("Some shit happened wrong when trying to base64 encode.")
		return None
	return b64sample

def shazam (payload=None):
	flavortext = {1:"I found this: ", 2:"Here's what I found: ", 3:"This is what I found: ", 4:"This is what came up: "}
	artist = ""
	songname = ""
	date = ""
	youtube = ""
	failed = False

	if payload is None:
		return "Something went wrong while trying to detect a song. Sorry :["
	url = "https://shazam.p.rapidapi.com/songs/detect"
	headers = {
		'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.77 Safari/537.36",
	    'x-rapidapi-host': "shazam.p.rapidapi.com",
	    'x-rapidapi-key': RapidAPIKey,
	    'content-type': "text/plain",
	    }
	response = requests.request("POST", url, data=payload, headers=headers)
	if str(response.status_code) == "503":
		print("Status code 503 occurred, trying again in 120 seconds")
		time.sleep(120)
		response = requests.request("POST", url, data=payload, headers=headers)
	try:
		response = response.json()
		try:
			songname = response['track']['urlparams']["{tracktitle}"]
			artist = response['track']['urlparams']["{trackartist}"]
		except KeyError as e:
			print("Fields for song name, artist, or date weren't available")
			failed = True
		except IndexError as e:
			print("The date did not exist.")
		try:
			date = response['track']['sections'][0]['metadata'][2]['text'] #functional, but commenting out since unused
		except Exception as e:
			print(e)
			print("Date exception triggered.")
			print(response)
		try:
			for option in response['track']['sections']: #Each option is an item in a list, and each item is a dictionary in this case
				if 'youtubeurl' in option: #response['track']['sections'][2]["youtubeurl"]["actions"][0]["uri"]
					for option2 in option['youtubeurl']['actions']: #Most likely this for loop will iterate only once as there will only be one option, but just in case
						if 'uri' in option2:
							youtube = option2['uri']
							#songdetail = option['share']['subject'] #This seems to yield shazam's guess at song artist and title basically
							break
					break
		except KeyError:
			print("Youtube link not available")
			failed = True
		try:
			for option in response['track']['hub']['providers']:
				if option['type'] == "SPOTIFY":
					for option2 in option['actions']:
						if option2['name'] == "hub:spotify:deeplink":
							spotifylink = option['actions'][0]['uri']
							break
					break
		except Exception as e:
			print("Probably there is no Spotify link if you're seeing this")
		if not failed:
			youtube = re.match(".*(?:youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=)([^#\&\?]*).*", youtube)
			prefix = "alanmun.github.io/WhatSongIsThat/?a="
			if youtube is not None: message = prefix + artist + "&s=" + songname + "&d=" + date + "&y=" + youtube.group(1)
			else: message = prefix + artist + "&s=" + songname + "&d=" + date + "&y="
			return flavortext[random.randint(1,4)] + message
		else:
			return "I couldn't find anything, sorry :["
		return response
	except ValueError:
		print("VALUE ERROR: ")
		print(response)
		return "I couldn't find anything, sorry :( Shazam servers may be down right now"
	except KeyError:
		print("KEY ERROR: ")
		print(response)
		return "I couldn't find anything, sorry :("

def scrapeStatusForVideo(statusURL):
	try:
		ydl = youtube_dl.YoutubeDL({'outtmpl': '%(id)s.%(ext)s'})
		with ydl:
			result = ydl.extract_info(
				statusURL,
				download=False # We only care about the video's URL
			)
		return result['url']
	except Exception as e:
		print(e)
		print("Exception triggered in scrapeStatusForVideo on url: " + str(statusURL))
		return None

if __name__ == '__main__':
	main()
