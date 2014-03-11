#!/usr/bin/env python

#	-----------------------------------------------------------------------------
#	"THE BEER-WARE LICENSE" (Revision 42):
#	<erikjolson@arych.com> wrote this file. As long as you retain this notice you
#	can do whatever you want with this stuff. If we meet some day, and you think
#	this stuff is worth it, you can buy me a beer in return. Erik J. Olson.
#	-----------------------------------------------------------------------------

#evernote pytidylib premailer

import email, gzip, hashlib, imaplib, json, urllib, urllib2, os, re, smtplib, string, StringIO, sys, time, traceback, uuid
import lxml.etree as et
import xml.etree.ElementTree as ET
from email.header import decode_header
from email.MIMEMultipart import MIMEMultipart
from email.MIMEText import MIMEText
from email.MIMEImage import MIMEImage
from evernote.api.client import EvernoteClient
import evernote.edam.type.ttypes as Types
import tidylib
from tidylib import tidy_document
#from boilerpipe.extract import Extractor
from premailer import transform

_config = None
_debug = False
_knowntags = None
_imaphost = None
_smtphost = None
_users = {}

def main():
	if len(sys.argv) != 2:
		print '\tUsage: ' + sys.argv[0] + ' config.xml'
	else:
		load_config(sys.argv[1])

		if _debug:
			os.system('clear')

		init_users()
		init_mail()
		init_tidy()

		notes = check_for_new_notes()
		if len(notes) > 0:
			debug('Processing notes...')

			for note in notes:
				process_note(note)

				if _debug and get_setting('debug/waitaftercreate/text()') == 'True':
					c = raw_input('Continue? ')
					if len(c) > 0 and c[0].lower() != 'y':
						break

def load_config(filename):
	global _config, _debug

	with open(filename, 'r') as file:
		data = file.read()

	_config = et.fromstring(data)
	_debug = True if get_setting('debug/@enabled') == 'True' else False
	
def check_for_new_notes():
	notes = []

	debug('Checking for new notes...')

	_imaphost.select(get_setting('email/incoming/folder/text()'))
	(resultcode, ids) = _imaphost.search(None, '(UNSEEN)')# % get_setting('email/incoming/address/text()'))
	if resultcode == 'OK' and len(ids[0]) > 0:
		for id in ids[0].split(' '):
			note = {
				'id': id,
				'user': None,
				'sender': None,
				'subject': None,
				'title': None,
				'shorttitle': None,
				'content': None,
				'tags': [],
				'link': None,
				'extension': None,
				'resources': None,
				'error': None
			}
			
			debug('Fetching email id %s...' % id, 1)
			(resultcode, data) = _imaphost.fetch(id, "(RFC822)")
			_imaphost.store(id, '-FLAGS', '\\Seen')

			message = email.message_from_string(data[0][1])
			if message.is_multipart():
				note['content'] = ''
				for part in message.walk():
					pBody = part.get_payload(decode=True)
					if pBody != None:
						note['content']+= '%s\n' % pBody
			else:
				note['content'] = message.get_payload(decode=True);

			to = email.utils.parseaddr(message['to'])
			note['user'] = get_username(to[1])
			note['sender'] = message['from']
			note['subject'] = decode_header(message['subject'])[0][0] if message['subject'] != None else ''
			
			debug(note['subject'][:75] + '...' if len(note['subject']) > 75 else note['subject'], 2)
			debug('Parsing title and tags from subject...', 2)

			if len(note['subject']) > 0:
				match = re.search(get_setting('email/title/text()'), note['subject']).groups()
				if len(match) > 0:
					note['title'] = match[0]
				else:
					note['title'] = note['subject']
				
				matches = re.findall(get_setting('email/tags/text()'), message['subject'])
				for match in matches:
					note['tags'].append(match.strip())

			debug('Parsing note link from body...', 2)

			match = re.search(get_setting('email/incoming/url/text()'), note['content']).groups()
			if len(match) > 0:
				note['link'] = match[0]
				note['extension'] = get_extension(note['link'])

			if get_setting('parsing/prefetch/text()') == 'True':
				notes.append(note)
			else:
				process_note(note)

	return notes

def process_note(note):
	debug('Processing %s // %s...' % (note['id'], note['title']), 1)

	try:
		get_user(note)
		if handle_error(note):
			return False

		if note['link'] != None:
			if get_setting('evernote/validresources/resource[text()="%s"]' % note['extension']) != None:
				embed_resource(note)
				if handle_error(note):
					return False
			else:
				simplify(note)
				if handle_error(note):
					return False

				embed_images(note)
				if handle_error(note):
					return False

				tag(note)
				if handle_error(note):
					return False
		else:
			text_to_html(note)
			if handle_error(note):
				return False

		if True or note['resources'] == None:
			sanitize(note)
			if handle_error(note):
				return False

		if _debug == False:
			print '%s << %s: "%s" (%s)' % (note['user'], note['id'], note['title'], note['sender'])

		save(note)
		if handle_error(note):
			return False

		remove_note(note)

		return True
	except Exception as e:
		note['error'] = '%s: %s' % (type(e), str(e))
		handle_error(note)
		return False

def text_to_html(note):
	debug('Converting text to html...', 2)

	note['content'] = note['content'].replace('\r\n', '<br/>')
	note['content'] = note['content'].replace('\t', '  ')
	note['content'] = note['content'].replace(' ', '&nbsp;')

def save(note):
	service = get_setting('evernote/@service')
	if service == 'email':
		note = email_to_evernote(note)
	elif service == 'api':
		note = save_to_evernote(note)

def email_to_evernote(note):
	debug('Emailing to evernote...', 2)

	message = MIMEMultipart('alternative')

	message['From'] = get_setting('evernote/email/from/text()')
	message['To'] = get_setting('evernote/email/to/text()')

	if note['tags'] != None:
		subject = note['title']
		for tag in note['tags']:
			subject += ' #%s' % tag
		message['Subject'] = subject
	else:
		message['Subject'] = note['title']
	
	body = re.sub(get_setting('email/safeemail/text()'), '', note['content'])

	part1 = MIMEText(re.sub('</?[a-zA-Z0-9]+/?>', '', body), 'plain')
	part2 = MIMEText(body, 'html')

	message.attach(part1)
	message.attach(part2)

	_smtphost.sendmail(message['From'], message['To'], message.as_string())


def save_to_evernote(note):
	debug('Saving for %s...' % note['user'], 2)

	nBody = '<?xml version="1.0" encoding="UTF-8"?>'
	nBody += '<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
	nBody += '<en-note>%s</en-note>' % note['content']
	
	nAttributes = Types.NoteAttributes()
	nAttributes.author = "Notilitus"
	nAttributes.sourceURL = note['link']

	nTags = []
	for sTag in note['tags']:
		if sTag not in _users[note['user']]['tags']:
			create_tag(_users[note['user']], sTag)

		nTags.append(_users[note['user']]['tags'][sTag])
	
	enote = Types.Note()
	enote.title = note['title']
	enote.attributes = nAttributes
	enote.content = nBody
	enote.tagGuids = nTags

	if note['resources'] != None:
		enote.resources = note['resources']

	_users[note['user']]['notes'].createNote(enote)

def simplify(note):
	if (len(note['extension']) == 0) or (get_setting('simplify/validextensions/extension[text()="%s"]' % note['extension']) != None):
		if get_setting('simplify/@service') == 'readability':
			readable = simplify_readability(note['link'])
			if 'error' in readable and readable['error'] == True:
				note['error'] = 'Parsing Error: %s' % readable['messages']
			else:
				note['content'] = readable['content']
				if note['title'] == None:
					note['title'] = readable['title']
			
		elif get_setting('simplify/@service') == 'boilerpipe':
			extractor = Extractor(extractor='ArticleExtractor', url=note['link'])
			note['title'] = 'boilerpipe'
			note['content'] = extractor.getHTML()
	else:
		note['content'] = note['link']

def simplify_readability(url):
	debug('Simplifying via readability...', 2)

	try:
		params = urllib.urlencode({
			'token': get_setting('simplify/key/text()'),
			'url': url
		})

		resourceurl = get_setting('simplify/url/text()') + '?' + params

		debug(url, 3)
				
		stream = urllib2.urlopen(resourceurl)
		sResponse = stream.read()
	except urllib2.HTTPError as e:
		sResponse = e.read()

	return json.loads(sResponse)

def sanitize(note):
	debug('Sanitizing note content...', 2)

	if get_setting('evernote/sanitize/@applytemplate') == 'True':
		with open(get_setting('evernote/sanitize/template/text()'), 'r') as file:
			template = file.read()
			template = template.replace('{content}', note['content'])
			
		note['content'] = transform(template)
		
		preservedElements = []
		preservePattern = get_setting('evernote/sanitize/preserve/pattern/text()')
		preserves = get_setting('evernote/sanitize/preserve/elements/text()').split(',')
		for preserve in preserves:
			matches = re.findall(preservePattern.format(preserve), note['content'])
			for match in matches:
				placeholder = '{%s}' % uuid.uuid4().hex
				preservedElements.append({'placeholder': placeholder, 'element': match})
				note['content'] = note['content'].replace(match, placeholder, 1)
	
		note['content'] = re.sub(get_setting('evernote/sanitize/attributes/empty/text()'), '', note['content'])
		note['content'] = re.sub(get_setting('evernote/sanitize/attributes/prohibited/text()'), '', note['content'])
		note['content'] = re.sub(get_setting('evernote/sanitize/elements/text()'), '', note['content'])
		note['content'] = note['content'].encode('utf-8', errors='ignore')
		(note['content'], errors) = tidy_document(note['content'])

		for element in preservedElements:
			note['content'] = note['content'].replace(element['placeholder'], element['element'])
	
	if note['title'] != None:
		note['title'] = note['title'].replace('\n', ' ').replace('\r', '').replace('  ', ' ')
	else:
		note['title'] = get_setting('evernote/sanitize/defaulttitle/text()')

def tag(note):
	if get_setting('evernote/tags/text()') == 'True':
		if get_setting('tagging/@service') == 'yahoo':
			response = tag_yahoo(note)
			for entity in response.findall('results/entities/entity'):
				tag = entity.find('text').text
				if tag not in note['tags']:
					note['tags'].append(tag.lower())
		elif get_setting('tagging/@service') == 'opencalais':
			response = tag_opencalais(note)

	sTags = ','.join(note['tags'])
	debug(sTags[:50] if len(sTags) > 50 else sTags, 3)

def tag_yahoo(note):
	debug('Tagging via yahoo...', 2)

	params = urllib.urlencode({
		'q': 'select * from contentanalysis.analyze where url = "%s"' % note['link']
	})

	stream = urllib2.urlopen(get_setting('tagging/service[@name="yahoo"]/url/text()'), params)
	sResponse = stream.read()
	sResponse = re.sub(' xmlns="[^"]+"', '', sResponse)
	response = ET.ElementTree(ET.fromstring(sResponse)).getroot()
	
	return response

def tag_opencalais(note):
	debug('Tagging via opencalais...', 2)

	headers = {
		'x-calais-licenseID': get_setting('tagging/service[@name="opencalais"]/key/text()'),
		'contentType': 'TEXT/HTML', #TEXT/RAW
		'outputFormat': 'Application/JSON',
		'enableMetadataType': 'SocialTags'
	}

	params = urllib.urlencode({
		'content': note['content']
	})

	request = urllib2.Request(get_setting('tagging/service[@name="opencalais"]/url/text()'), params, headers)
	stream = urllib2.urlopen(request)
	sResponse = stream.read()
	print sResponse
	exit()
	response = json.loads(sResponse)
	return sResponse

def embed_images(note):
	if _users[note['user']]['embedResources']:
		debug('Embedding images...', 2)

		if note['resources'] == None:
			note['resources'] = []

		matches = re.findall(get_setting('evernote/embed/images/text()'), note['content'])
		for match in matches:
			resource = get_resource(match[1])
			note['resources'].append(resource)
			note['content'] = note['content'].replace(
				match[0],
				'<en-media type="%s" hash="%s"/>' % (resource.mime, resource.data.bodyHash)
			)

def embed_resource(note):
	if  _users[note['user']]['embedResources'] and note['link'] != None:
		if note['resources'] == None:
			note['resources'] = []

		resource = get_resource(note['link'])
		
		note['content'] = '<en-media type="%s" hash="%s"/>' % (resource.mime, resource.data.bodyHash)
		note['resources'].append(resource)
	else:
		note['content'] = note['link']

def get_resource(link):
	debug('Fetching resource...', 3)

	request = urllib2.Request(link)
	request.add_header('Accept-encoding', 'gzip')
	stream = urllib2.urlopen(request)
	mime = stream.info().getheader('Content-Type')

	if stream.info().get('Content-Encoding') == 'gzip':
		debug('Decompressing resource...', 3)
		buf = StringIO.StringIO(stream.read())
		f = gzip.GzipFile(fileobj=buf)
		data = f.read()
	else:
		data = stream.read()

	if get_setting('debug/savelastresource/text()') == 'True':
		localFile = open('lastresource.jpg', 'wb')
		localFile.write(data)
		localFile.close()

	hasher = hashlib.md5()
	hasher.update(data)
	md5hash = hasher.hexdigest()
	
	edata = Types.Data()
	edata.bodyHash = md5hash
	edata.size = len(data)
	edata.body = data

	resource = Types.Resource()
	resource.data = edata
	resource.mime = mime

	return resource

def get_mime(extension):
	if extension in ['jpg', 'jpeg']:
		return 'image/jpeg'
	elif extension == 'gif':
		return 'image/gif'
	elif extension == 'png':
		return 'image/png'
	elif extension == 'pdf':
		return 'application/pdf'
	else:
		return None

def get_user(note):	
	if note['user'] not in _users:
		debug('Initializing evernote connection for %s...' % note['user'])

		if get_setting('evernote/@sandbox') == 'True':
			client = EvernoteClient(token=get_setting('evernote/token[@type="sandbox"]/text()'))
		else:
			if note['user'] == 'arychj':
				client = EvernoteClient(token=get_setting('evernote/token/text()'), sandbox=False)
			else:
				note['error'] = 'User "%s" not found' % note['user']

		_users[note['user']] = {
			'client': client,
			'notes': client.get_note_store(),
			'embedResources': True,
			'tags': None
		}

		get_user_tags(_users[note['user']]);

		#_evernote = EvernoteClient(
		#	consumer_key = get_setting('evernote/key/text()'),
		#	consumer_secret = get_setting('evernote/secret/text()'),
		#	sandbox = False
		#)

def remove_note(note):
	debug('Removing note from queue...', 2)

	if get_setting('email/incoming/deletecompleted/text()') == 'True':
		_imaphost.store(note['id'], '+FLAGS', '\\Deleted')
	elif get_setting('email/incoming/markasread/text()') == 'True':
		_imaphost.store(note['id'], '+FLAGS', '\\Seen')

def get_setting(path):
	setting =  _config.xpath(path)
	return None if len(setting) == 0 else setting[0]

def handle_error(note):
	if note['error'] == None:
		return False
	else:
		debug('\nException:\n%s\n\n%s' % (note['error'], traceback.format_exc()))
		
		message = MIMEMultipart('alternative')

		message['From'] = get_setting('evernote/email/from/text()')
		message['To'] = note['sender']
		message['Subject'] = 'Error processing "%s"' % note['subject']
		
		body = '%s\nMessage ID:%s\n\nOriginal Message:\n--------\n%s' % (note['error'], note['id'], note['content'])
		body = re.sub(get_setting('email/safeemail/text()'), '', body)

		part1 = MIMEText(re.sub('</?[a-zA-Z0-9]+/?>', '', body), 'plain')

		message.attach(part1)

		_smtphost.sendmail(message['From'], message['To'], message.as_string())

		if get_setting('debug/haltonerror/text()') == 'True':
			exit()
		else:
			return True

def init_mail():
	global _imaphost, _smtphost

	if 'port' not in _config.find('email/imaphost').attrib: 
		host = get_setting('email/imaphost/text()')
	else:
		host = get_setting('email/imaphost/text()') + ':' + get_setting('email/imaphost/@port')
	
	debug('Initializing IMAP connection to %s...' % host)

	_imaphost = imaplib.IMAP4_SSL(host)
	_imaphost.login(get_setting('email/credentials/username/text()'), get_setting('email/credentials/password/text()'))

	if 'port' not in _config.find('email/smtphost').attrib: 
		host = get_setting('email/smtphost/text()')
	else:
		host = get_setting('email/smtphost/text()') + ':' + get_setting('email/smtphost/@port')
	
	debug('Initializing SMTP connection to %s...' % host)
	
	_smtphost = smtplib.SMTP(host)
	_smtphost.starttls()
	_smtphost.login(get_setting('email/credentials/username/text()'), get_setting('email/credentials/password/text()'))

def init_users():
	global _users

def init_tidy():
	tidylib.BASE_OPTIONS = {
		'output-xhtml': 1,
		'doctype': 'omit',
		'show-body-only': 1,
		'clean': 0,
		'hide-comments': 1,
		'drop-proprietary-attributes': 1,
		'indent': 1,
		'force-output': 1,
		'join-styles': 0,
		'wrap': 0
	}

def cleanup():
	_imaphost.expunge()
	_imaphost.close()
	_smtphost.quit()

def debug(message, level=0):
	if _debug:
		print ('  ' * level + '> ' if level > 0 else '') + (message if message != None else 'None')

def get_user_tags(user):
	tags = {}
	etags = user['notes'].listTags()
	for etag in etags:
		tags[etag.name.lower()] = etag.guid
	
	user['tags'] = tags

def get_extension(link):
	extension = None
	if link != None:
		link = link[-5:]
		dot = link.rfind('.')
		if dot > -1:
			extension = link[link.rfind('.'):].strip('.')
		else:
			extension = ''
	return extension

def create_tag(user, tag):
	etag = Types.Tag()
	etag.name = tag
	etag = user['notes'].createTag(etag)
	user['tags'][tag.lower()] = etag.guid

def flatten(d):
	flat = ''
	for key in d.keys():
		flat = flat + '&' + str(key) + '=' + str(d[key])

	return flat[1:]

def get_username(address):
	matches = re.findall(get_setting('email/incoming/user/text()'), address)
	if len(matches) == 1:
		return matches[0]
	else:
		return address

main()
